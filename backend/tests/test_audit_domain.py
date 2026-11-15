from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from jsonschema import Draft202012Validator

from app.api.dependencies import get_current_user
from app.core.contracts import ToolContext, UserContext
from app.core.exceptions import ValidationError
from app.domain_plugins.audit import register
from app.domain_plugins.audit.analysis import (
    RULE_ORDER,
    RULES,
    analyze_transactions,
    generate_audit_report,
)
from app.domain_plugins.audit.excel import parse_transactions
from app.domain_plugins.audit.router import router as audit_router
from app.domain_plugins.audit.tools import TOOL_NAMES, build_descriptors
from app.domain_plugins.audit.tools.report import generate_audit_report_tool

# The audit domain pack has no ORM models; the conftest app lifespan creates
# the platform schema before any integration test runs.

SEED_DIR = Path(__file__).resolve().parents[1] / "seed"
SAMPLE_FILE = SEED_DIR / "audit_sample.xlsx"
LABELS_FILE = SEED_DIR / "audit_labels.json"

LABELED_RULES = {"duplicate_payment", "large_transaction", "rapid_repeat"}


def _load_labels() -> dict[str, Any]:
    return json.loads(LABELS_FILE.read_text(encoding="utf-8"))


def _payload() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    parsed = parse_transactions(SAMPLE_FILE.read_bytes(), SAMPLE_FILE.name)
    analysis = analyze_transactions(parsed["transactions"])
    labels = _load_labels()
    return parsed, analysis, labels


def test_parse_sample_workbook() -> None:
    parsed = parse_transactions(SAMPLE_FILE.read_bytes(), SAMPLE_FILE.name)
    assert parsed["header_row"] == 2
    assert parsed["field_mapping"] == {
        "date": "交易日期",
        "payment_id": "付款单号",
        "vendor_code": "供应商编码",
        "vendor": "供应商",
        "invoice_no": "发票号",
        "amount": "金额",
        "description": "摘要",
        "department": "部门",
        "bank_account": "银行账号",
    }
    transactions = parsed["transactions"]
    assert 60 <= len(transactions) <= 100
    assert len({tx["row_number"] for tx in transactions}) == len(transactions)
    assert all(tx["row_number"] >= 3 for tx in transactions)
    assert all(tx["transaction_id"] == f"T{tx['row_number']:04d}" for tx in transactions)

    labels = _load_labels()
    by_row = {tx["row_number"]: tx for tx in transactions}
    for label in labels["anomalies"]:
        assert label["row_number"] in by_row
        assert by_row[label["row_number"]]["payment_id"] == label["payment_id"]

    issues = parsed["quality_issues"]
    assert any(issue["issue"] == "unparsable_amount" for issue in issues)
    assert any(issue["issue"] == "missing_field" and issue["field"] == "payment_id" for issue in issues)
    assert not any(tx["description"] and "合计" in tx["description"] for tx in transactions)


def test_csv_alias_parsing_and_skipped_rows() -> None:
    csv_data = (
        "公司交易明细\n"
        "交易日期,供应商,金额,付款单号,发票号,摘要\n"
        "2024-01-05,测试供应商,1234.50,P001,FP001,测试采购\n"
        "\n"
        "合计,,1234.50,,,\n"
    ).encode("utf-8-sig")
    parsed = parse_transactions(csv_data, "transactions.csv")
    assert parsed["header_row"] == 2
    assert len(parsed["transactions"]) == 1
    transaction = parsed["transactions"][0]
    assert transaction["row_number"] == 3
    assert transaction["amount"] == 1234.5
    assert transaction["vendor"] == "测试供应商"

    english = (
        b"date,vendor,amount,payment_id,invoice_no\n"
        b"2024-02-03,ACME,88.00,P002,INV-2\n"
    )
    parsed_english = parse_transactions(english, "english.csv")
    assert parsed_english["field_mapping"]["date"] == "date"
    assert parsed_english["transactions"][0]["vendor"] == "ACME"


def test_detection_metrics_against_labels() -> None:
    _, analysis, labels = _payload()
    expected = {
        (item["rule"], item["row_number"])
        for item in labels["anomalies"]
        if item["rule"] in LABELED_RULES
    }
    detected = {
        (item["rule"], item["row_number"])
        for item in analysis["anomalies"]
        if item["rule"] in LABELED_RULES
    }
    true_positives = detected & expected
    precision = len(true_positives) / len(detected) if detected else 1.0
    recall = len(true_positives) / len(expected) if expected else 1.0
    print(f"audit detection precision={precision:.3f} recall={recall:.3f}")
    assert len(expected) >= 10
    assert recall >= 0.9, f"recall={recall:.3f}, missing={sorted(expected - detected)}"
    assert precision >= 0.8, f"precision={precision:.3f}, extra={sorted(detected - expected)}"

    counts = analysis["summary"]["by_rule"]
    assert counts["duplicate_payment"] >= 1
    assert counts["large_transaction"] >= 1
    assert counts["rapid_repeat"] >= 1
    assert analysis["summary"]["total_transactions"] == len(analysis["transactions"])


def test_every_anomaly_references_valid_source_row() -> None:
    parsed, analysis, _ = _payload()
    by_row = {tx["row_number"]: tx for tx in parsed["transactions"]}
    for anomaly in analysis["anomalies"]:
        row = anomaly["row_number"]
        assert row in by_row, anomaly
        source = by_row[row]
        assert anomaly["transaction_id"] == source["transaction_id"]
        assert anomaly["vendor"] == source["vendor"]
        assert anomaly["amount"] == source["amount"]
        assert anomaly["date"] == source["date"]
        assert anomaly["description"] == source["description"]
        assert anomaly["rule"] in RULE_ORDER
        assert anomaly["severity"] in {"low", "medium", "high", "critical"}
        assert isinstance(anomaly["evidence"], dict)
        if anomaly["rule"] == "duplicate_payment":
            original = by_row[anomaly["evidence"]["duplicate_of_row"]]
            assert original["vendor"] == source["vendor"]
            assert original["amount"] == source["amount"]
            assert (
                original["invoice_no"] == source["invoice_no"]
                or original["payment_id"] == source["payment_id"]
            )
        if anomaly["rule"] == "rapid_repeat":
            assert anomaly["evidence"]["payments_in_window"] >= 3
            assert len(anomaly["evidence"]["related_rows"]) >= 3


def test_analysis_is_deterministic() -> None:
    first = parse_transactions(SAMPLE_FILE.read_bytes(), SAMPLE_FILE.name)
    second = parse_transactions(SAMPLE_FILE.read_bytes(), SAMPLE_FILE.name)
    assert first == second
    assert analyze_transactions(first["transactions"]) == analyze_transactions(
        second["transactions"]
    )
    dump_one = json.dumps(analyze_transactions(first["transactions"]), ensure_ascii=False, sort_keys=True)
    dump_two = json.dumps(analyze_transactions(first["transactions"]), ensure_ascii=False, sort_keys=True)
    assert dump_one == dump_two


def test_configurable_options() -> None:
    transactions = [
        {
            "row_number": 3,
            "transaction_id": "T0003",
            "date": "2024-01-01",
            "vendor": "供应商甲",
            "vendor_code": "V1",
            "amount": 50_000.0,
            "payment_id": "P1",
            "invoice_no": "I1",
            "description": "",
        },
        {
            "row_number": 4,
            "transaction_id": "T0004",
            "date": "2024-01-02",
            "vendor": "供应商甲",
            "vendor_code": "V1",
            "amount": 50_000.0,
            "payment_id": "P2",
            "invoice_no": "I2",
            "description": "",
        },
        {
            "row_number": 5,
            "transaction_id": "T0005",
            "date": "2024-01-03",
            "vendor": "供应商甲",
            "vendor_code": "V1",
            "amount": 50_000.0,
            "payment_id": "P3",
            "invoice_no": "I3",
            "description": "",
        },
    ]
    default = analyze_transactions(transactions)
    assert default["summary"]["by_rule"]["large_transaction"] == 0
    assert default["summary"]["by_rule"]["rapid_repeat"] == 1

    tuned = analyze_transactions(transactions, large_threshold=50_000, window_days=10)
    assert tuned["summary"]["by_rule"]["large_transaction"] == 3
    assert tuned["options"] == {"large_threshold": 50_000.0, "window_days": 10}

    with pytest.raises(ValidationError):
        analyze_transactions(transactions, window_days=0)


def test_duplicate_and_quality_rules() -> None:
    transactions = [
        {
            "row_number": 3,
            "transaction_id": "T0003",
            "date": "2024-01-01",
            "vendor": "供应商甲",
            "vendor_code": "V1",
            "amount": 100.0,
            "payment_id": "P1",
            "invoice_no": "I1",
            "description": "",
        },
        {
            "row_number": 4,
            "transaction_id": "T0004",
            "date": "2024-01-09",
            "vendor": "供应商甲",
            "vendor_code": "V1",
            "amount": 100.0,
            "payment_id": "P2",
            "invoice_no": "I1",
            "description": "",
        },
    ]
    result = analyze_transactions(transactions)
    duplicates = [item for item in result["anomalies"] if item["rule"] == "duplicate_payment"]
    assert len(duplicates) == 1
    assert duplicates[0]["row_number"] == 4
    assert duplicates[0]["evidence"]["duplicate_of_row"] == 3

    quality = analyze_transactions(
        [
            {
                **transactions[0],
                "amount": -5.0,
                "payment_id": "DUP",
            },
            {
                **transactions[1],
                "payment_id": "DUP",
                "vendor": "供应商乙",
                "amount": 7.0,
            },
        ]
    )
    quality_anomalies = [
        item for item in quality["anomalies"] if item["rule"] == "data_quality"
    ]
    assert [item["row_number"] for item in quality_anomalies] == [3, 4]
    assert "negative_amount" in quality_anomalies[0]["evidence"]["issues"]
    assert "duplicate_payment_id" in quality_anomalies[1]["evidence"]["issues"]


def test_report_contains_rules_and_source_rows() -> None:
    parsed, analysis, _ = _payload()
    markdown = generate_audit_report(
        parsed["transactions"],
        analysis["anomalies"],
        parsed["quality_issues"],
        max_anomalies=10,
    )
    for rule in RULE_ORDER:
        assert rule in markdown
    for rule in RULES:
        assert rule["name"] in markdown
    assert "免责声明" in markdown
    assert "来源行引用" in markdown
    assert "金额待定" in markdown

    first = analysis["anomalies"][0]
    assert f"第 {first['row_number']} 行" in markdown
    assert "| 行号 |" in markdown
    # Top-anomaly table is capped while source references keep all rows.
    assert markdown.count("| duplicate_payment |") <= 10


class FakeRegistry:
    def __init__(self) -> None:
        self.entries: dict[str, tuple[Any, Any]] = {}

    def __contains__(self, name: object) -> bool:
        return name in self.entries

    def register(self, descriptor: Any, handler: Any) -> None:
        assert descriptor.name not in self.entries
        self.entries[descriptor.name] = (descriptor, handler)

    def unregister(self, name: str) -> None:
        self.entries.pop(name, None)


def test_tool_registration_is_idempotent() -> None:
    registry = FakeRegistry()
    first = register(registry)
    second = register(registry)

    assert [descriptor.name for descriptor in first] == list(TOOL_NAMES)
    assert [descriptor.name for descriptor in second] == list(TOOL_NAMES)
    assert len(registry.entries) == 2

    descriptors = build_descriptors()
    for descriptor in descriptors:
        assert descriptor.source_type == "native"
        assert descriptor.risk_level == "low"
        assert descriptor.timeout_seconds == 60
        assert descriptor.permission_scope == ["audit:read"]
        assert descriptor.requires_approval is False
        assert descriptor.enabled is True
        assert descriptor.input_schema["type"] == "object"
        Draft202012Validator.check_schema(descriptor.input_schema)
        assert descriptor.name in registry.entries

    analyze_schema = next(
        item.input_schema for item in descriptors if item.name == "analyze_transactions"
    )
    assert analyze_schema["required"] == ["artifact_id"]
    assert analyze_schema["properties"]["options"]["properties"]["window_days"]["minimum"] == 1
    report_schema = next(
        item.input_schema for item in descriptors if item.name == "generate_audit_report"
    )
    assert "oneOf" in report_schema
    assert set(report_schema["properties"]) == {
        "artifact_id",
        "analysis",
        "options",
        "max_anomalies",
    }


@pytest.mark.asyncio
async def test_report_tool_from_analysis_payload() -> None:
    parsed, analysis, _ = _payload()
    payload = {**parsed, **analysis}
    ctx = ToolContext(
        user=UserContext(user_id="u1", username="auditor", permissions={"audit:read"}),
        tool_name="generate_audit_report",
    )
    result = await generate_audit_report_tool(
        {"analysis": payload, "max_anomalies": 5}, ctx
    )
    assert result["format"] == "markdown"
    assert result["truncated"] is False
    assert "交易异常审计分析报告" in result["markdown"]
    assert result["summary"]["total_transactions"] == len(parsed["transactions"])
    assert result["summary"]["by_rule"]["duplicate_payment"] >= 1


@pytest.mark.asyncio
async def test_analyze_tool_executes_uploaded_artifact(client, admin, db) -> None:
    from app.core.contracts import ToolInvocation
    from app.modules.permission.service import PermissionService
    from app.modules.tool.service import ToolService

    register()
    service = ToolService(db)
    await service.sync_native()

    upload = await client.post(
        "/api/v1/artifacts",
        headers=admin,
        files={
            "file": (
                SAMPLE_FILE.name,
                SAMPLE_FILE.read_bytes(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    assert upload.status_code == 201, upload.text
    artifact_id = upload.json()["id"]

    permissions = PermissionService(db)
    user = await permissions.repo.get_user_by_username("admin")
    assert user is not None
    actor = await permissions.load_context(user.id)

    analysis_result = await service.execute(
        ToolInvocation(
            tool_name="analyze_transactions",
            arguments={"artifact_id": artifact_id},
            user_id=actor.user_id,
        ),
        actor,
    )
    assert analysis_result.ok is True, analysis_result.error
    output = analysis_result.output
    assert output["artifact_id"] == artifact_id
    assert output["filename"] == SAMPLE_FILE.name
    assert "transactions" not in output
    assert output["summary"]["total_transactions"] == len(_payload()[0]["transactions"])
    assert output["summary"]["by_rule"]["duplicate_payment"] >= 1
    assert output["quality_issues"]

    report_result = await service.execute(
        ToolInvocation(
            tool_name="generate_audit_report",
            arguments={"artifact_id": artifact_id, "max_anomalies": 5},
            user_id=actor.user_id,
        ),
        actor,
    )
    assert report_result.ok is True, report_result.error
    report = report_result.output
    assert report["format"] == "markdown"
    assert "交易异常审计分析报告" in report["markdown"]
    assert report["summary"]["total_transactions"] == output["summary"]["total_transactions"]


@pytest.mark.asyncio
async def test_router_endpoints() -> None:
    app = FastAPI()
    app.include_router(audit_router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id="u1", username="auditor", permissions={"audit:read"}
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        rules = await client.get("/api/v1/audit/rules")
        assert rules.status_code == 200, rules.text
        body = rules.json()
        assert body["total"] == len(RULES) == 4
        assert {item["rule"] for item in body["rules"]} == set(RULE_ORDER)
        assert body["sample_file_available"] is True
        assert body["sample_labels_available"] is True

        sample = await client.get("/api/v1/audit/sample-file")
        assert sample.status_code == 200
        assert sample.content.startswith(b"PK\x03\x04")
        assert sample.headers["content-type"].startswith(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
