"""Native tool descriptors for the audit domain pack."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from app.core.contracts import ToolDescriptor
from app.domain_plugins.audit.tools.analyze import analyze_transactions_tool
from app.domain_plugins.audit.tools.report import generate_audit_report_tool

TOOL_NAMES = ("analyze_transactions", "generate_audit_report")

_CALLABLE = Callable[[dict[str, Any], Any], Awaitable[Any]]

_ANALYSIS_OPTIONS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "large_threshold": {
            "type": "number",
            "minimum": 0,
            "description": "大额交易阈值（元），默认 100000",
        },
        "window_days": {
            "type": "integer",
            "minimum": 1,
            "maximum": 365,
            "description": "短期重复付款窗口天数，默认 7",
        },
    },
    "additionalProperties": False,
}

_TRANSACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "row_number": {"type": "integer", "minimum": 1},
        "transaction_id": {"type": "string"},
        "date": {"type": ["string", "null"]},
        "vendor": {"type": ["string", "null"]},
        "vendor_code": {"type": ["string", "null"]},
        "amount": {"type": ["number", "null"]},
        "payment_id": {"type": ["string", "null"]},
        "invoice_no": {"type": ["string", "null"]},
        "description": {"type": ["string", "null"]},
        "department": {"type": ["string", "null"]},
        "bank_account": {"type": ["string", "null"]},
    },
    "required": ["row_number", "transaction_id", "amount"],
    "additionalProperties": False,
}

_ANOMALY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rule": {
            "type": "string",
            "enum": [
                "duplicate_payment",
                "large_transaction",
                "rapid_repeat",
                "data_quality",
            ],
        },
        "severity": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
        "row_number": {"type": "integer", "minimum": 1},
        "transaction_id": {"type": ["string", "null"]},
        "vendor": {"type": ["string", "null"]},
        "amount": {"type": ["number", "null"]},
        "date": {"type": ["string", "null"]},
        "description": {"type": ["string", "null"]},
        "evidence": {"type": "object", "additionalProperties": True},
    },
    "required": ["rule", "severity", "row_number", "evidence"],
    "additionalProperties": False,
}

_ANALYZE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "artifact_id": {
            "type": "string",
            "minLength": 1,
            "description": "已上传的 xlsx/xlsm/csv 交易文件 artifact_id",
        },
        "options": _ANALYSIS_OPTIONS_SCHEMA,
        "include_transactions": {
            "type": "boolean",
            "default": False,
            "description": "是否在结果中返回全部交易明细；默认只返回异常与汇总",
        },
    },
    "required": ["artifact_id"],
    "additionalProperties": False,
}

_ANALYZE_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "artifact_id": {"type": "string"},
        "filename": {"type": "string"},
        "header_row": {"type": "integer", "minimum": 1},
        "field_mapping": {"type": "object", "additionalProperties": {"type": "string"}},
        "quality_issues": {"type": "array", "items": {"type": "object"}},
        "transactions": {"type": "array", "items": _TRANSACTION_SCHEMA},
        "anomalies": {"type": "array", "items": _ANOMALY_SCHEMA},
        "summary": {"type": "object"},
        "options": {"type": "object"},
    },
    "required": [
        "artifact_id",
        "field_mapping",
        "anomalies",
        "summary",
        "options",
    ],
}

_REPORT_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "artifact_id": {
            "type": "string",
            "minLength": 1,
            "description": "交易文件 artifact_id，与 analysis 二选一",
        },
        "analysis": {
            "type": "object",
            "description": "此前 analyze_transactions 的输出，与 artifact_id 二选一",
            "properties": {
                "transactions": {"type": "array", "items": {"type": "object"}},
                "anomalies": {"type": "array", "items": {"type": "object"}},
                "quality_issues": {"type": "array", "items": {"type": "object"}},
                "summary": {"type": "object"},
                "options": {"type": "object"},
            },
            "required": ["transactions"],
            "additionalProperties": True,
        },
        "options": _ANALYSIS_OPTIONS_SCHEMA,
        "max_anomalies": {
            "type": "integer",
            "minimum": 1,
            "maximum": 100,
            "default": 20,
            "description": "报告中重点异常明细的最大条数",
        },
    },
    "oneOf": [
        {"required": ["artifact_id"]},
        {"required": ["analysis"]},
    ],
    "additionalProperties": False,
}

_REPORT_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "format": {"type": "string", "enum": ["markdown"]},
        "markdown": {"type": "string"},
        "truncated": {"type": "boolean"},
        "summary": {"type": "object"},
        "artifact_id": {"type": "string"},
    },
    "required": ["format", "markdown", "truncated", "summary"],
}


class _ToolRegistry(Protocol):
    def register(self, descriptor: ToolDescriptor, handler: _CALLABLE) -> None: ...

    def unregister(self, name: str) -> None: ...

    def __contains__(self, name: object) -> bool: ...


def build_descriptors() -> list[ToolDescriptor]:
    return [
        ToolDescriptor(
            name="analyze_transactions",
            description=(
                "对上传的财务交易 Excel/CSV 执行确定性审计分析：重复付款、大额交易、"
                "短期重复付款、数据质量检查。返回结构化交易行、异常列表与统计汇总，"
                "不返回原始文件字节。"
            ),
            input_schema=_ANALYZE_INPUT_SCHEMA,
            output_schema=_ANALYZE_OUTPUT_SCHEMA,
            version="1.0.0",
            source_type="native",
            risk_level="low",
            timeout_seconds=60,
            permission_scope=["audit:read"],
            requires_approval=False,
            enabled=True,
        ),
        ToolDescriptor(
            name="generate_audit_report",
            description=(
                "基于此前分析结果或上传的交易文件生成 Markdown 格式审计分析报告，"
                "包含规则命中汇总、重点异常明细、数据质量问题、来源行引用与免责声明。"
            ),
            input_schema=_REPORT_INPUT_SCHEMA,
            output_schema=_REPORT_OUTPUT_SCHEMA,
            version="1.0.0",
            source_type="native",
            risk_level="low",
            timeout_seconds=60,
            permission_scope=["audit:read"],
            requires_approval=False,
            enabled=True,
        ),
    ]


def tool_handlers() -> dict[str, _CALLABLE]:
    return {
        "analyze_transactions": analyze_transactions_tool,
        "generate_audit_report": generate_audit_report_tool,
    }


def register_audit_tools(registry: _ToolRegistry) -> list[ToolDescriptor]:
    """Register audit tools; safe to call repeatedly."""

    descriptors = build_descriptors()
    handlers = tool_handlers()
    for descriptor in descriptors:
        if descriptor.name in registry:
            registry.unregister(descriptor.name)
        registry.register(descriptor, handlers[descriptor.name])
    return descriptors


__all__ = [
    "TOOL_NAMES",
    "build_descriptors",
    "register_audit_tools",
    "tool_handlers",
]
