"""Deterministic audit rules and markdown report generation.

Pure module: no database or network dependencies. Every rule is a pure
function of the parsed transactions, so repeated runs produce identical output.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date
from typing import Any

from app.core.exceptions import ValidationError
from app.domain_plugins.audit.excel import REQUIRED_FIELDS, normalize_text

RULE_DUPLICATE = "duplicate_payment"
RULE_LARGE = "large_transaction"
RULE_RAPID = "rapid_repeat"
RULE_QUALITY = "data_quality"

RULE_ORDER = (RULE_DUPLICATE, RULE_LARGE, RULE_RAPID, RULE_QUALITY)
RULE_NAMES = {
    RULE_DUPLICATE: "重复付款",
    RULE_LARGE: "大额交易",
    RULE_RAPID: "短期重复付款",
    RULE_QUALITY: "数据质量",
}
DEFAULT_LARGE_THRESHOLD = 100_000.0
DEFAULT_WINDOW_DAYS = 7

RULES: list[dict[str, Any]] = [
    {
        "rule": RULE_DUPLICATE,
        "name": RULE_NAMES[RULE_DUPLICATE],
        "description": (
            "同一供应商（优先供应商编码，其次规范化名称）在金额相同的前提下，"
            "发票号相同或付款单号相同的记录，除首条外标记为重复付款。"
        ),
        "severity": "high",
        "default_options": {},
    },
    {
        "rule": RULE_LARGE,
        "name": RULE_NAMES[RULE_LARGE],
        "description": "交易金额大于等于阈值（默认 100000 元）的大额交易。",
        "severity": "high",
        "default_options": {"large_threshold": DEFAULT_LARGE_THRESHOLD},
    },
    {
        "rule": RULE_RAPID,
        "name": RULE_NAMES[RULE_RAPID],
        "description": (
            "同一供应商在窗口期（默认 7 天）内出现 3 笔及以上付款时，"
            "将窗口内第 3 笔及之后的付款标记为短期重复付款。"
        ),
        "severity": "medium",
        "default_options": {"window_days": DEFAULT_WINDOW_DAYS},
    },
    {
        "rule": RULE_QUALITY,
        "name": RULE_NAMES[RULE_QUALITY],
        "description": "缺少必需字段、金额为负数或付款单号重复的数据质量问题。",
        "severity": "medium",
        "default_options": {},
    },
]


def _vendor_key(transaction: dict[str, Any]) -> str | None:
    code = normalize_text(transaction.get("vendor_code"))
    if code:
        return f"code:{code.lower()}"
    vendor = normalize_text(transaction.get("vendor"))
    if vendor:
        return f"name:{vendor.lower()}"
    return None


def _amount_cents(transaction: dict[str, Any]) -> int | None:
    amount = transaction.get("amount")
    if amount is None:
        return None
    try:
        return int(round(float(amount) * 100))
    except (TypeError, ValueError):
        return None


def _payment_key(transaction: dict[str, Any], field: str) -> str | None:
    value = normalize_text(transaction.get(field))
    return value.lower() if value else None


def _as_date(transaction: dict[str, Any]) -> date | None:
    raw = transaction.get("date")
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        return None


def _anomaly(
    transaction: dict[str, Any],
    *,
    rule: str,
    severity: str,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    return {
        "rule": rule,
        "severity": severity,
        "row_number": transaction.get("row_number"),
        "transaction_id": transaction.get("transaction_id"),
        "vendor": transaction.get("vendor"),
        "amount": transaction.get("amount"),
        "date": transaction.get("date"),
        "description": transaction.get("description"),
        "evidence": evidence,
    }


def _validate_options(
    large_threshold: float | None, window_days: int | None
) -> tuple[float, int]:
    threshold = DEFAULT_LARGE_THRESHOLD if large_threshold is None else large_threshold
    window = DEFAULT_WINDOW_DAYS if window_days is None else window_days
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        raise ValidationError("large_threshold 必须是数字")
    if threshold < 0:
        raise ValidationError("large_threshold 不能为负数")
    if isinstance(window, bool) or not isinstance(window, int):
        raise ValidationError("window_days 必须是整数")
    if window < 1:
        raise ValidationError("window_days 必须大于等于 1")
    return float(threshold), int(window)


def _duplicate_components(ordered: list[dict[str, Any]]) -> list[list[int]]:
    parent = list(range(len(ordered)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    for field in ("invoice_no", "payment_id"):
        seen: dict[str, int] = {}
        for index, transaction in enumerate(ordered):
            key = _payment_key(transaction, field)
            if not key:
                continue
            if key in seen:
                union(seen[key], index)
            else:
                seen[key] = index

    components: dict[int, list[int]] = defaultdict(list)
    for index in range(len(ordered)):
        components[find(index)].append(index)
    return [sorted(indices) for indices in components.values()]


def _duplicate_anomalies(transactions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for transaction in transactions:
        vendor = _vendor_key(transaction)
        cents = _amount_cents(transaction)
        if vendor is None or cents is None:
            continue
        groups[(vendor, cents)].append(transaction)

    anomalies: list[dict[str, Any]] = []
    for group in groups.values():
        if len(group) < 2:
            continue
        ordered = sorted(group, key=lambda item: item["row_number"])
        for indices in _duplicate_components(ordered):
            if len(indices) < 2:
                continue
            first = ordered[indices[0]]
            related_rows = [ordered[index]["row_number"] for index in indices]
            group_invoices = {
                _payment_key(ordered[index], "invoice_no") for index in indices
            }
            shared_invoice = len({value for value in group_invoices if value}) == 1 and any(
                group_invoices
            )
            matched_on = "invoice_no" if shared_invoice else "payment_id"
            for index in indices[1:]:
                transaction = ordered[index]
                anomalies.append(
                    _anomaly(
                        transaction,
                        rule=RULE_DUPLICATE,
                        severity="high",
                        evidence={
                            "matched_on": matched_on,
                            "vendor_key": _vendor_key(transaction),
                            "invoice_no": transaction.get("invoice_no"),
                            "payment_id": transaction.get("payment_id"),
                            "duplicate_of_row": first["row_number"],
                            "duplicate_of_payment_id": first.get("payment_id"),
                            "related_rows": related_rows,
                        },
                    )
                )
    return anomalies


def _large_anomalies(
    transactions: list[dict[str, Any]], threshold: float
) -> list[dict[str, Any]]:
    anomalies: list[dict[str, Any]] = []
    for transaction in transactions:
        amount = transaction.get("amount")
        if amount is None:
            continue
        if float(amount) >= threshold:
            anomalies.append(
                _anomaly(
                    transaction,
                    rule=RULE_LARGE,
                    severity="high",
                    evidence={
                        "threshold": threshold,
                        "amount": round(float(amount), 2),
                        "excess": round(float(amount) - threshold, 2),
                    },
                )
            )
    return anomalies


def _rapid_anomalies(
    transactions: list[dict[str, Any]], window_days: int
) -> list[dict[str, Any]]:
    by_vendor: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for transaction in transactions:
        vendor = _vendor_key(transaction)
        when = _as_date(transaction)
        if vendor is None or when is None:
            continue
        by_vendor[vendor].append(transaction)

    anomalies: list[dict[str, Any]] = []
    for group in by_vendor.values():
        ordered = sorted(group, key=lambda item: (_as_date(item), item["row_number"]))
        for position, transaction in enumerate(ordered):
            current = _as_date(transaction)
            assert current is not None
            prior = [
                candidate
                for candidate in ordered[:position]
                if 0 <= (current - _as_date(candidate)).days <= window_days  # type: ignore[operator]
            ]
            if len(prior) < 2:
                continue
            window_rows = [candidate["row_number"] for candidate in prior][-10:] + [
                transaction["row_number"]
            ]
            anomalies.append(
                _anomaly(
                    transaction,
                    rule=RULE_RAPID,
                    severity="medium",
                    evidence={
                        "window_days": window_days,
                        "payments_in_window": len(prior) + 1,
                        "window_start": prior[0]["date"],
                        "window_end": transaction["date"],
                        "related_rows": window_rows,
                        "payment_ids": [
                            candidate.get("payment_id") for candidate in prior
                        ][-10:]
                        + [transaction.get("payment_id")],
                    },
                )
            )
    return anomalies


def _quality_anomalies(transactions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    anomalies: list[dict[str, Any]] = []
    seen_payment: dict[str, dict[str, Any]] = {}
    for transaction in transactions:
        issues: list[str] = []
        fields: list[str] = []
        for field in REQUIRED_FIELDS:
            value = transaction.get(field)
            if value is None or value == "":
                issues.append(f"missing_{field}")
                fields.append(field)
        amount = transaction.get("amount")
        if amount is not None and float(amount) < 0:
            issues.append("negative_amount")
            fields.append("amount")

        duplicate_of: dict[str, Any] | None = None
        payment_key = _payment_key(transaction, "payment_id")
        if payment_key:
            if payment_key in seen_payment:
                duplicate_of = seen_payment[payment_key]
                issues.append("duplicate_payment_id")
                fields.append("payment_id")
            else:
                seen_payment[payment_key] = transaction

        if not issues:
            continue
        severity = "low" if all(item.startswith("missing_") for item in issues) else "medium"
        evidence: dict[str, Any] = {
            "issues": issues,
            "fields": sorted(set(fields)),
        }
        if duplicate_of is not None:
            evidence["duplicate_of_row"] = duplicate_of["row_number"]
            evidence["duplicate_of_transaction_id"] = duplicate_of.get("transaction_id")
        anomalies.append(
            _anomaly(
                transaction,
                rule=RULE_QUALITY,
                severity=severity,
                evidence=evidence,
            )
        )
    return anomalies


def analyze_transactions(
    transactions: list[dict[str, Any]],
    *,
    large_threshold: float | None = None,
    window_days: int | None = None,
) -> dict[str, Any]:
    """Run all deterministic audit rules over parsed transactions."""

    threshold, window = _validate_options(large_threshold, window_days)
    ordered = sorted(transactions, key=lambda item: item.get("row_number") or 0)
    anomalies = [
        *_duplicate_anomalies(ordered),
        *_large_anomalies(ordered, threshold),
        *_rapid_anomalies(ordered, window),
        *_quality_anomalies(ordered),
    ]
    anomalies.sort(key=lambda item: (item["row_number"], RULE_ORDER.index(item["rule"])))

    by_rule = {rule: 0 for rule in RULE_ORDER}
    by_severity = {"low": 0, "medium": 0, "high": 0, "critical": 0}
    for anomaly in anomalies:
        by_rule[anomaly["rule"]] += 1
        by_severity[anomaly["severity"]] += 1

    total_amount = round(
        sum(float(item["amount"]) for item in ordered if item.get("amount") is not None), 2
    )
    return {
        "transactions": ordered,
        "anomalies": anomalies,
        "summary": {
            "total_transactions": len(ordered),
            "total_amount": total_amount,
            "anomaly_count": len(anomalies),
            "by_rule": by_rule,
            "by_severity": by_severity,
        },
        "options": {
            "large_threshold": threshold,
            "window_days": window,
        },
    }


def _cell(value: Any, limit: int = 60) -> str:
    if value is None or value == "":
        return "-"
    text = str(value).replace("|", "\\|").replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _money(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value):,.2f}"


def _severity_label(severity: str) -> str:
    return {"low": "低", "medium": "中", "high": "高", "critical": "严重"}.get(severity, severity)


def generate_audit_report(
    transactions: list[dict[str, Any]],
    anomalies: list[dict[str, Any]],
    quality_issues: list[dict[str, Any]] | None = None,
    *,
    large_threshold: float | None = None,
    window_days: int | None = None,
    max_anomalies: int = 20,
) -> str:
    """Render a deterministic markdown audit report."""

    threshold, window = _validate_options(large_threshold, window_days)
    quality_issues = quality_issues or []
    ordered_transactions = sorted(transactions, key=lambda item: item.get("row_number") or 0)
    ordered_anomalies = sorted(
        anomalies, key=lambda item: (item.get("row_number") or 0, RULE_ORDER.index(item["rule"]))
    )
    total_amount = round(
        sum(
            float(item["amount"])
            for item in ordered_transactions
            if item.get("amount") is not None
        ),
        2,
    )
    by_rule = {rule: 0 for rule in RULE_ORDER}
    for anomaly in ordered_anomalies:
        by_rule[anomaly["rule"]] += 1

    lines: list[str] = [
        "# 交易异常审计分析报告",
        "",
        "## 一、分析概览",
        "",
        "| 指标 | 数值 |",
        "| --- | --- |",
        f"| 交易笔数 | {len(ordered_transactions)} |",
        f"| 交易总金额（元） | {_money(total_amount)} |",
        f"| 异常数量 | {len(ordered_anomalies)} |",
        f"| 数据质量问题 | {len(quality_issues)} |",
        f"| 大额阈值（元） | {_money(threshold)} |",
        f"| 短期付款窗口（天） | {window} |",
        "",
        "## 二、规则命中汇总",
        "",
        "| 规则 | 名称 | 数量 | 说明 |",
        "| --- | --- | --- | --- |",
    ]
    for rule in RULES:
        lines.append(
            f"| {rule['rule']} | {rule['name']} | {by_rule[rule['rule']]} | "
            f"{_cell(rule['description'], 80)} |"
        )

    lines.extend(
        [
            "",
            f"## 三、重点异常明细（前 {max_anomalies} 条）",
            "",
            "| 行号 | 规则 | 严重程度 | 供应商 | 金额（元） | 日期 | 摘要 | 证据 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    ranked = sorted(
        ordered_anomalies,
        key=lambda item: (
            -{"low": 1, "medium": 2, "high": 3, "critical": 4}.get(item["severity"], 0),
            item.get("amount") is None,
            -(float(item.get("amount") or 0)),
            item.get("row_number") or 0,
        ),
    )
    for anomaly in ranked[: max(1, max_anomalies)]:
        evidence = json.dumps(anomaly.get("evidence") or {}, ensure_ascii=False, sort_keys=True)
        lines.append(
            "| {row} | {rule} | {severity} | {vendor} | {amount} | {date} | {desc} | {evidence} |".format(
                row=anomaly.get("row_number"),
                rule=anomaly["rule"],
                severity=_severity_label(anomaly["severity"]),
                vendor=_cell(anomaly.get("vendor")),
                amount=_money(anomaly.get("amount")),
                date=_cell(anomaly.get("date")),
                desc=_cell(anomaly.get("description")),
                evidence=_cell(evidence, 120),
            )
        )
    if not ordered_anomalies:
        lines.append("| - | - | - | - | - | - | - | 未发现规则命中的异常 |")

    lines.extend(["", "## 四、数据质量问题", ""])
    if quality_issues:
        lines.extend(["| 行号 | 字段 | 问题 | 原值 |", "| --- | --- | --- | --- |"])
        for issue in quality_issues:
            lines.append(
                f"| {issue.get('row_number')} | {_cell(issue.get('field'))} | "
                f"{_cell(issue.get('message') or issue.get('issue'))} | "
                f"{_cell(issue.get('value'))} |"
            )
    else:
        lines.append("未发现解析层面的数据质量问题。")

    lines.extend(["", "## 五、来源行引用", ""])
    if ordered_anomalies:
        for anomaly in ordered_anomalies:
            lines.append(
                f"- 第 {anomaly.get('row_number')} 行：{anomaly['rule']}，"
                f"供应商 {_cell(anomaly.get('vendor'))}，"
                f"金额 {_money(anomaly.get('amount'))} 元"
            )
    else:
        lines.append("- 无")

    lines.extend(
        [
            "",
            "## 免责声明",
            "",
            "本报告由确定性规则引擎基于上传的交易数据自动生成，仅用于审计线索筛查，"
            "不构成审计结论。请结合原始凭证、合同与业务背景进行人工复核。",
            "",
        ]
    )
    return "\n".join(lines)
