"""``generate_audit_report`` native tool."""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.contracts import ToolContext
from app.core.exceptions import ToolError
from app.domain_plugins.audit.analysis import (
    RULE_ORDER,
    analyze_transactions,
    generate_audit_report,
)
from app.domain_plugins.audit.excel import parse_transactions
from app.domain_plugins.audit.tools._common import (
    load_artifact_bytes,
    parse_options,
    require_context_user,
)

MAX_REPORT_CHARS = 12_000


def _as_list(analysis: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = analysis.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ToolError(f"analysis.{key} 必须是数组")
    return [item for item in value if isinstance(item, dict)]


def _report_from_analysis(
    analysis: dict[str, Any],
    *,
    options: dict[str, Any],
    max_anomalies: int,
) -> str:
    transactions = _as_list(analysis, "transactions")
    if not transactions:
        raise ToolError("analysis.transactions 不能为空")
    anomalies = analysis.get("anomalies")
    quality_issues = _as_list(analysis, "quality_issues")
    if anomalies is None:
        computed = analyze_transactions(transactions, **options)
        anomalies = computed["anomalies"]
    elif not isinstance(anomalies, list):
        raise ToolError("analysis.anomalies 必须是数组")

    effective = {
        "large_threshold": options.get("large_threshold"),
        "window_days": options.get("window_days"),
    }
    stored = analysis.get("options")
    if isinstance(stored, dict):
        for key in effective:
            if effective[key] is None and stored.get(key) is not None:
                effective[key] = stored[key]

    return generate_audit_report(
        transactions,
        [item for item in anomalies if isinstance(item, dict)],
        quality_issues,
        large_threshold=effective["large_threshold"],
        window_days=effective["window_days"],
        max_anomalies=max_anomalies,
    )


def _summary(analysis: dict[str, Any]) -> dict[str, Any]:
    stored = analysis.get("summary")
    if isinstance(stored, dict) and "total_transactions" in stored:
        return stored
    transactions = _as_list(analysis, "transactions")
    anomalies = _as_list(analysis, "anomalies")
    by_rule = {rule: 0 for rule in RULE_ORDER}
    for anomaly in anomalies:
        rule = anomaly.get("rule")
        if rule in by_rule:
            by_rule[rule] += 1
    total_amount = round(
        sum(
            float(item["amount"])
            for item in transactions
            if isinstance(item.get("amount"), (int, float))
        ),
        2,
    )
    return {
        "total_transactions": len(transactions),
        "total_amount": total_amount,
        "anomaly_count": len(anomalies),
        "by_rule": by_rule,
    }


async def generate_audit_report_tool(
    arguments: dict[str, Any], ctx: ToolContext
) -> dict[str, Any]:
    artifact_id = arguments.get("artifact_id")
    analysis = arguments.get("analysis")
    if artifact_id is not None and analysis is not None:
        raise ToolError("artifact_id 与 analysis 只能提供其中一个")
    if artifact_id is None and analysis is None:
        raise ToolError("必须提供 artifact_id 或 analysis 之一")

    max_anomalies = arguments.get("max_anomalies", 20)
    if isinstance(max_anomalies, bool) or not isinstance(max_anomalies, int):
        raise ToolError("max_anomalies 必须是整数")
    if not 1 <= max_anomalies <= 100:
        raise ToolError("max_anomalies 必须在 1 到 100 之间")

    options = parse_options(arguments)
    resolved_artifact: str | None = None

    if analysis is not None:
        if not isinstance(analysis, dict):
            raise ToolError("analysis 必须是对象")
        markdown = _report_from_analysis(
            analysis, options=options, max_anomalies=max_anomalies
        )
        summary = _summary(analysis)
    else:
        if not isinstance(artifact_id, str) or not artifact_id.strip():
            raise ToolError("artifact_id 必须是非空字符串")
        resolved_artifact = artifact_id.strip()
        require_context_user(ctx)
        try:
            data, filename = await load_artifact_bytes(resolved_artifact, ctx)
        except ToolError:
            raise
        except Exception as exc:  # noqa: BLE001 - execution module raises domain errors
            raise ToolError(f"读取交易文件失败: {exc}") from exc
        try:
            parsed = await asyncio.to_thread(parse_transactions, data, filename)
        except Exception as exc:  # noqa: BLE001 - parser raises ValidationError
            raise ToolError(str(exc)) from exc
        result = await asyncio.to_thread(analyze_transactions, parsed["transactions"], **options)
        markdown = generate_audit_report(
            result["transactions"],
            result["anomalies"],
            parsed["quality_issues"],
            large_threshold=result["options"]["large_threshold"],
            window_days=result["options"]["window_days"],
            max_anomalies=max_anomalies,
        )
        summary = result["summary"]

    truncated = len(markdown) > MAX_REPORT_CHARS
    if truncated:
        markdown = (
            markdown[: MAX_REPORT_CHARS - 80]
            + "\n\n> 报告内容过长，已按长度限制截断，请结合明细数据复核。\n"
        )

    payload: dict[str, Any] = {
        "format": "markdown",
        "markdown": markdown,
        "truncated": truncated,
        "summary": summary,
    }
    if resolved_artifact:
        payload["artifact_id"] = resolved_artifact
    return payload
