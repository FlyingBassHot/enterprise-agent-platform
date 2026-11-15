"""MCP client transport for admin-managed MCP servers.

The module is the only place that talks to the MCP SDK. Everything that leaves
it is plain Python: normalized tool definitions and JSON-safe call results, so
no SDK model ever reaches the registry, the API or an execution record.

Secrets are never stored in the database: ``env``/``headers`` values are
templates whose ``${TOOL_SECRET_*}`` references are resolved here, at connect
time, and only in the child process / outbound request.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx2
from mcp import ClientSession, types
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.modules.tool.adapters.http import resolve_env_refs
from app.modules.tool.exceptions import (
    ToolDefinitionError,
    ToolExecutionError,
    ToolTimeoutError,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids ORM import at runtime
    from app.modules.tool.models import MCPServer

MAX_RESULT_CHARS = 20_000
MAX_ERROR_CHARS = 1_000
MAX_PAGES = 50
DEFAULT_TIMEOUT_SECONDS = 60.0
_HTTP_SCHEMES = {"http", "https"}


class MCPToolDefinition(BaseModel):
    """Remote tool normalized into tool-registry terms."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)


def _timeout(value: float | None) -> float:
    if value is not None:
        return max(1.0, float(value))
    return max(1.0, float(get_settings().mcp_call_timeout_seconds or DEFAULT_TIMEOUT_SECONDS))


def _validated_url(value: str | None) -> str:
    if not value:
        raise ToolDefinitionError("MCP HTTP transport requires a url")
    value = value.strip()
    try:
        parts = urlsplit(value)
        parts.port  # noqa: B018 - raises ValueError on a malformed port
    except ValueError as exc:
        raise ToolDefinitionError("MCP server url is not a valid absolute URL") from exc
    if parts.scheme not in _HTTP_SCHEMES or not parts.hostname:
        raise ToolDefinitionError("MCP server url must be an absolute http(s) URL")
    if parts.username or parts.password:
        raise ToolDefinitionError("MCP server url must not embed credentials")
    return value


def _resolved_mapping(values: Mapping[str, Any] | None, label: str) -> dict[str, str]:
    resolved: dict[str, str] = {}
    for name, value in (values or {}).items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise ToolDefinitionError(f"MCP server {label} must map strings to strings")
        resolved[name] = resolve_env_refs(value)
    return resolved


def _stdio_parameters(server: MCPServer) -> StdioServerParameters:
    command = (server.command or "").strip()
    if not command:
        raise ToolDefinitionError(f"MCP server {server.name} has no stdio command")
    args = [str(item) for item in (server.args or [])]
    return StdioServerParameters(
        command=command,
        args=args,
        env=_resolved_mapping(server.env, "env"),
    )


def _exception_leaf(exc: BaseException) -> BaseException:
    current = exc
    while isinstance(current, BaseExceptionGroup) and current.exceptions:
        current = current.exceptions[0]
    return current


def _transport_error(exc: BaseException, server: MCPServer, action: str) -> Exception:
    leaf = _exception_leaf(exc)
    if isinstance(leaf, TimeoutError):
        return ToolTimeoutError(
            f"MCP server {server.name} timed out while {action}"
        )
    if isinstance(leaf, ToolExecutionError | ToolDefinitionError | ToolTimeoutError):
        return leaf
    text = str(leaf) or type(leaf).__name__
    return ToolExecutionError(
        f"MCP server {server.name} failed while {action}: {text[:MAX_ERROR_CHARS]}"
    )


@asynccontextmanager
async def _open_session(server: MCPServer, timeout: float) -> AsyncIterator[ClientSession]:
    """Open an initialized MCP session, closing the transport deterministically."""

    if server.transport == "http":
        url = _validated_url(server.url)
        headers = _resolved_mapping(server.headers, "headers")
        client = httpx2.AsyncClient(
            headers=headers or None,
            timeout=httpx2.Timeout(
                connect=min(timeout, 30.0),
                read=timeout,
                write=timeout,
                pool=timeout,
            ),
        )
        async with client:
            async with streamable_http_client(url, http_client=client) as (
                read_stream,
                write_stream,
            ):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    yield session
            # Exhaust anyio's task-group cleanup before leaving the client.
        return

    if server.transport != "stdio":
        raise ToolDefinitionError(f"Unsupported MCP transport: {server.transport}")

    params = _stdio_parameters(server)
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            yield session


def _schema(value: Any, *, default_object: bool) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {"type": "object", "properties": {}} if default_object else {}


def _normalized_tool(raw: types.Tool) -> MCPToolDefinition:
    name = (raw.name or "").strip()
    if not name:
        raise ToolDefinitionError("MCP server advertised a tool without a name")
    description = (raw.description or "").strip()
    input_schema = _schema(raw.input_schema, default_object=True)
    if input_schema.get("type", "object") != "object":
        raise ToolDefinitionError(f"MCP tool {name} input schema must describe an object")
    output_schema = _schema(raw.output_schema, default_object=False)
    if output_schema and output_schema.get("type", "object") != "object":
        raise ToolDefinitionError(f"MCP tool {name} output schema must describe an object")
    return MCPToolDefinition(
        name=name,
        description=description,
        input_schema=input_schema,
        output_schema=output_schema,
    )


async def list_mcp_tools(
    server: MCPServer, timeout: float | None = None
) -> list[MCPToolDefinition]:
    """Discover every tool advertised by an MCP server.

    Raises :class:`ToolExecutionError` (or ``ToolTimeoutError``) on transport or
    protocol failure; the caller decides how to persist the failure.
    """

    limit = _timeout(timeout)
    discovered: list[MCPToolDefinition] = []
    try:
        async with asyncio.timeout(limit):
            async with _open_session(server, limit) as session:
                cursor: str | None = None
                seen_cursors: set[str] = set()
                for _ in range(MAX_PAGES):
                    params = (
                        types.PaginatedRequestParams(cursor=cursor) if cursor else None
                    )
                    result = await session.list_tools(params=params)
                    discovered.extend(_normalized_tool(tool) for tool in result.tools)
                    cursor = result.next_cursor
                    if not cursor or cursor in seen_cursors:
                        break
                    seen_cursors.add(cursor)
    except ToolDefinitionError:
        raise
    except Exception as exc:  # noqa: BLE001 - transport failures are normalized
        raise _transport_error(exc, server, "listing tools") from exc
    return discovered


def _text_content(content: list[Any]) -> str:
    parts = [
        block.text
        for block in content
        if isinstance(block, types.TextContent) and block.text
    ]
    return "\n".join(parts)


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    if isinstance(value, BaseModel):
        return _json_safe(value.model_dump(mode="json", exclude_none=True))
    return str(value)


def _truncate_result(value: Any) -> Any:
    if isinstance(value, str):
        if len(value) <= MAX_RESULT_CHARS:
            return value
        marker = "\n...[truncated]"
        return value[: MAX_RESULT_CHARS - len(marker)] + marker
    serialized = json.dumps(value, ensure_ascii=False, default=str)
    if len(serialized) <= MAX_RESULT_CHARS:
        return value
    return {
        "truncated": True,
        "preview": serialized[:MAX_RESULT_CHARS],
    }


def normalize_call_result(result: types.CallToolResult) -> Any:
    """Convert an SDK call result into a JSON-safe value.

    Structured content wins; otherwise text blocks are concatenated. Non-text
    content is only surfaced when no text exists, as JSON-safe dumps.
    """

    text = _text_content(list(result.content or []))
    if result.is_error:
        message = text or "MCP tool reported an error"
        raise ToolExecutionError(message[:MAX_ERROR_CHARS])

    if result.structured_content is not None:
        return _truncate_result(_json_safe(result.structured_content))
    if text:
        return _truncate_result(text)

    blocks = [
        block.model_dump(mode="json", exclude_none=True)
        for block in result.content or []
        if isinstance(block, BaseModel)
    ]
    if not blocks:
        return None
    return _truncate_result(_json_safe(blocks))


async def call_mcp_tool(
    server: MCPServer,
    remote_name: str,
    arguments: Mapping[str, Any] | None = None,
    timeout: float | None = None,
) -> Any:
    """Invoke a remote MCP tool and return a normalized, JSON-safe result."""

    name = (remote_name or "").strip()
    if not name:
        raise ToolDefinitionError("MCP tool name is required")
    payload = dict(arguments or {})
    limit = _timeout(timeout)
    try:
        async with asyncio.timeout(limit):
            async with _open_session(server, limit) as session:
                result = await session.call_tool(name, payload)
    except ToolDefinitionError:
        raise
    except Exception as exc:  # noqa: BLE001 - transport failures are normalized
        raise _transport_error(exc, server, f"calling tool {name}") from exc
    if not isinstance(result, types.CallToolResult):
        raise ToolExecutionError(
            f"MCP tool {name} returned an unsupported result type: {type(result).__name__}"
        )
    return normalize_call_result(result)


__all__ = [
    "MCPToolDefinition",
    "call_mcp_tool",
    "list_mcp_tools",
    "normalize_call_result",
]
