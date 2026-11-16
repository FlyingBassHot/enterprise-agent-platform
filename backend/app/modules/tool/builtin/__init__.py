"""Platform-owned baseline tools shared by every Agent.

These are not domain capabilities: they are the minimum an Agent needs to be
useful out of the box (know the time, see an uploaded artifact, reach an
allowlisted service). Handlers are code; the API can never install one.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from app.core.contracts import ToolDescriptor
from app.modules.tool.builtin.artifacts import read_artifact_tool
from app.modules.tool.builtin.http import http_request_tool
from app.modules.tool.builtin.time import current_time_tool

TOOL_NAMES = (
    "platform_current_time",
    "platform_read_artifact",
    "platform_http_request",
)

_CALLABLE = Callable[[dict[str, Any], Any], Awaitable[Any]]

_CURRENT_TIME_OUTPUT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "timezone": {"type": "string"},
        "iso8601": {"type": "string"},
        "date": {"type": "string"},
        "weekday": {"type": "string"},
        "utc_offset": {"type": "string"},
        "epoch_seconds": {"type": "integer"},
    },
    "required": ["timezone", "iso8601", "date", "weekday", "utc_offset", "epoch_seconds"],
}

_READ_ARTIFACT_OUTPUT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "artifact_id": {"type": "string"},
        "filename": {"type": "string"},
        "content_type": {"type": "string"},
        "size": {"type": "integer"},
        "content": {"type": ["string", "null"]},
        "truncated": {"type": "boolean"},
        "total_chars": {"type": "integer"},
        "note": {"type": "string"},
    },
    "required": ["artifact_id", "filename", "size", "content", "truncated", "total_chars"],
}

_HTTP_OUTPUT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "integer"},
        "url": {"type": "string"},
        "content_type": {"type": "string"},
        "location": {"type": "string"},
        "text": {"type": "string"},
        "truncated": {"type": "boolean"},
    },
    "required": ["status", "url", "content_type", "location", "text", "truncated"],
}


class _ToolRegistry(Protocol):
    def register(self, descriptor: ToolDescriptor, handler: _CALLABLE) -> None: ...

    def unregister(self, name: str) -> None: ...

    def __contains__(self, name: object) -> bool: ...


def build_descriptors() -> list[ToolDescriptor]:
    return [
        ToolDescriptor(
            name="platform_current_time",
            description=(
                "返回平台当前时间。模型自身无法获知真实时间，需要日期、星期或时间戳时"
                "必须调用本工具。支持 IANA 时区（例如 Asia/Shanghai），默认 UTC。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "tz": {
                        "type": "string",
                        "maxLength": 64,
                        "description": "IANA 时区名，例如 Asia/Shanghai；默认 UTC",
                    }
                },
                "additionalProperties": False,
            },
            output_schema=_CURRENT_TIME_OUTPUT,
            version="1.0.0",
            source_type="native",
            risk_level="low",
            timeout_seconds=10,
            permission_scope=[],
            requires_approval=False,
            enabled=True,
        ),
        ToolDescriptor(
            name="platform_read_artifact",
            description=(
                "读取用户上传的附件内容。文本类附件（txt/md/csv 等）返回正文摘录，"
                "二进制附件只返回元数据。只能读取当前用户有权访问的附件。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "artifact_id": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 64,
                        "description": "附件 ID，来自用户上传记录",
                    },
                    "max_chars": {
                        "type": "integer",
                        "minimum": 500,
                        "maximum": 20000,
                        "default": 8000,
                        "description": "返回正文的最大字符数",
                    },
                },
                "required": ["artifact_id"],
                "additionalProperties": False,
            },
            output_schema=_READ_ARTIFACT_OUTPUT,
            version="1.0.0",
            source_type="native",
            risk_level="low",
            timeout_seconds=30,
            permission_scope=[],
            requires_approval=False,
            enabled=True,
        ),
        ToolDescriptor(
            name="platform_http_request",
            description=(
                "向管理员白名单内的 HTTPS/HTTP 服务发起 GET 或 POST 请求，用于查询"
                "外部系统数据。白名单为空时禁止出网；内网与保留地址一律拒绝；"
                "不自动跟随重定向。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "method": {
                        "type": "string",
                        "enum": ["GET", "POST"],
                        "default": "GET",
                    },
                    "url": {"type": "string", "minLength": 1, "maxLength": 2048},
                    "headers": {
                        "type": "object",
                        "maxProperties": 20,
                        "additionalProperties": {"type": "string", "maxLength": 1024},
                    },
                    "json": {
                        "type": "object",
                        "additionalProperties": True,
                        "description": "POST 请求体（JSON 对象）",
                    },
                    "timeout_seconds": {
                        "type": "number",
                        "minimum": 1,
                        "maximum": 30,
                        "default": 20,
                    },
                },
                "required": ["url"],
                "additionalProperties": False,
            },
            output_schema=_HTTP_OUTPUT,
            version="1.0.0",
            source_type="native",
            risk_level="medium",
            timeout_seconds=40,
            permission_scope=[],
            requires_approval=False,
            enabled=True,
        ),
    ]


def tool_handlers() -> dict[str, _CALLABLE]:
    return {
        "platform_current_time": current_time_tool,
        "platform_read_artifact": read_artifact_tool,
        "platform_http_request": http_request_tool,
    }


def register_builtin_tools(registry: _ToolRegistry) -> list[ToolDescriptor]:
    """Register the baseline tools; safe to call repeatedly."""

    descriptors = build_descriptors()
    handlers = tool_handlers()
    for descriptor in descriptors:
        if descriptor.name in registry:
            registry.unregister(descriptor.name)
        registry.register(descriptor, handlers[descriptor.name])
    return descriptors


def register(registry: Any = None) -> list[ToolDescriptor]:
    """Entry point used by the app lifespan (mirrors domain plugin packs)."""

    if registry is None:
        from app.modules.tool.registry import get_native_registry

        registry = get_native_registry()
    return register_builtin_tools(registry)


__all__ = [
    "TOOL_NAMES",
    "build_descriptors",
    "register",
    "register_builtin_tools",
    "tool_handlers",
]
