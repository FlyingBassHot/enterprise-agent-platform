from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.modules.tool.exceptions import (
    ToolDefinitionError,
    ToolExecutionError,
    ToolTimeoutError,
)

ENV_REF_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
MAX_ERROR_BODY = 500


def resolve_env_refs(template: str) -> str:
    """Substitute ``${NAME}`` references; missing variables fail closed."""

    def _replace(match: re.Match[str]) -> str:
        from app.core.config import get_settings

        name = match.group(1)
        prefix = get_settings().tool_secret_env_prefix
        if not name.startswith(prefix):
            raise ToolExecutionError(
                f"Environment reference {name} is not allowed; use the {prefix} prefix"
            )
        value = os.environ.get(name)
        if value is None:
            raise ToolExecutionError(f"Environment variable {name} is not configured")
        return value

    return ENV_REF_PATTERN.sub(_replace, template)


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def _resolved_headers(raw: Any) -> dict[str, str]:
    if not isinstance(raw, Mapping):
        raise ToolDefinitionError("HTTP tool config headers must be an object")
    headers: dict[str, str] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise ToolDefinitionError("HTTP tool config headers must map strings to strings")
        headers[name] = resolve_env_refs(value)
    return headers


def _target(config: Mapping[str, Any], arguments: Mapping[str, Any]) -> tuple[str, str, dict, dict]:
    method = str(config.get("method", "GET")).upper()
    if method not in ALLOWED_METHODS:
        raise ToolDefinitionError(f"Unsupported HTTP method: {method}")
    url = config.get("url")
    if not isinstance(url, str):
        raise ToolDefinitionError("HTTP tool config requires a url")
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ToolDefinitionError("HTTP tool url must be an absolute http(s) URL")
    if parts.username or parts.password:
        raise ToolDefinitionError("HTTP tool url must not embed credentials")

    raw_query = config.get("query") or {}
    if not isinstance(raw_query, Mapping):
        raise ToolDefinitionError("HTTP tool config query must be an object")
    query = {str(key): _stringify(value) for key, value in raw_query.items()}

    if method in {"GET", "DELETE"}:
        query.update({str(key): _stringify(value) for key, value in arguments.items()})
    body_mode = str(config.get("body_mode", "json"))
    if method not in {"GET", "DELETE"} and body_mode not in {"json", "form"}:
        raise ToolDefinitionError(f"Unsupported body_mode: {body_mode}")
    return method, url, query, arguments


async def execute_http_tool(
    config: Mapping[str, Any],
    arguments: Mapping[str, Any],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout_seconds: float = 30.0,
) -> Any:
    method, url, query, body = _target(config, arguments)
    headers = _resolved_headers(config.get("headers") or {})
    body_mode = str(config.get("body_mode", "json"))

    request_kwargs: dict[str, Any] = {"params": query or None, "headers": headers}
    if method not in {"GET", "DELETE"}:
        if body_mode == "form":
            request_kwargs["data"] = {
                str(key): _stringify(value) for key, value in body.items()
            }
        else:
            request_kwargs["json"] = dict(body)

    try:
        async with httpx.AsyncClient(
            transport=transport,
            timeout=timeout_seconds,
            follow_redirects=False,
        ) as client:
            response = await client.request(method, url, **request_kwargs)
    except httpx.TimeoutException as exc:
        raise ToolTimeoutError(
            f"HTTP tool timed out after {timeout_seconds}s"
        ) from exc
    except httpx.HTTPError as exc:
        raise ToolExecutionError(
            f"HTTP request failed: {type(exc).__name__}: {exc}"
        ) from exc

    if not response.is_success:
        snippet = response.text[:MAX_ERROR_BODY]
        raise ToolExecutionError(f"HTTP {response.status_code} from tool endpoint: {snippet}")

    content_type = response.headers.get("content-type", "").lower()
    if "json" in content_type:
        try:
            return response.json()
        except ValueError:
            return response.text
    return response.text
