"""Allowlisted outbound HTTP tool.

Egress is deny-by-default: the request only proceeds when the target host is
listed in ``APP_HTTP_TOOL_ALLOWED_HOSTS`` *and* every resolved address is
globally routable. Redirects are not followed automatically, so a 3xx response
is returned to the model as data instead of silently leaving the allowlist.

Residual risk: DNS is resolved twice (once for the IP guard, once by the HTTP
client), so a rebinding resolver can still win the race between the two.
Operators should only allowlist hosts they control.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.core.config import get_settings
from app.core.contracts import ToolContext
from app.core.exceptions import ToolError

ALLOWED_METHODS = {"GET", "POST"}
FORBIDDEN_HEADERS = {"connection", "content-length", "host", "transfer-encoding"}
DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_TIMEOUT_SECONDS = 30.0
DEFAULT_HEADERS = {
    "accept": "application/json, text/plain;q=0.9, */*;q=0.8",
    "user-agent": "enterprise-agent-platform/0.1 (+platform_http_request)",
}


def build_client(timeout_seconds: float) -> httpx.AsyncClient:
    """Separate constructor so tests can inject a deterministic transport."""

    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout_seconds),
        follow_redirects=False,
    )


async def _resolve_addresses(host: str, port: int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    infos = await asyncio.get_running_loop().getaddrinfo(
        host, port, type=socket.SOCK_STREAM
    )
    return [ipaddress.ip_address(info[4][0]) for info in infos]


def _host_allowed(host: str, entries: list[str]) -> bool:
    for raw in entries:
        entry = (raw or "").strip().lower()
        if not entry:
            continue
        if entry.startswith("."):
            if host.endswith(entry):
                return True
        elif host == entry:
            return True
    return False


async def http_request_tool(arguments: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    settings = get_settings()
    method = str(arguments.get("method") or "GET").upper()
    if method not in ALLOWED_METHODS:
        raise ToolError(f"Unsupported method {method}; allowed: {', '.join(sorted(ALLOWED_METHODS))}")
    url = str(arguments.get("url") or "").strip()
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise ToolError("Only http and https URLs are allowed")
    host = (parts.hostname or "").lower()
    if not host:
        raise ToolError("URL must include a host")
    if parts.username or parts.password:
        raise ToolError("URL credentials are not allowed; use an allowlisted host instead")
    allowlist = list(settings.http_tool_allowed_hosts)
    if not allowlist:
        raise ToolError(
            "Outbound HTTP is disabled: APP_HTTP_TOOL_ALLOWED_HOSTS is empty"
        )
    if not _host_allowed(host, allowlist):
        raise ToolError(f"Host '{host}' is not in APP_HTTP_TOOL_ALLOWED_HOSTS")
    headers = _headers(arguments.get("headers"))
    body = None
    if method == "POST":
        body = arguments.get("json")
        if not isinstance(body, dict):
            raise ToolError("POST requires a JSON object body in 'json'")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        addresses = await _resolve_addresses(host, port)
    except OSError as exc:
        raise ToolError(f"Could not resolve host '{host}': {exc}") from exc
    if not addresses:
        raise ToolError(f"Host '{host}' resolved to no addresses")
    blocked = sorted({str(address) for address in addresses if not address.is_global})
    if blocked:
        raise ToolError(f"Refusing non-public address(es) for '{host}': {', '.join(blocked)}")

    timeout = min(
        float(arguments.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS),
        MAX_TIMEOUT_SECONDS,
    )
    limit = max(1, int(settings.http_tool_max_response_kb)) * 1024
    payload = bytearray()
    truncated = False
    status = 0
    content_type = ""
    location = ""
    async with build_client(timeout) as client:
        async with client.stream(method, url, headers=headers, json=body) as response:
            status = response.status_code
            content_type = response.headers.get("content-type", "")
            location = response.headers.get("location", "")
            async for chunk in response.aiter_bytes():
                room = limit - len(payload)
                if len(chunk) > room:
                    payload.extend(chunk[:room])
                    truncated = True
                    break
                payload.extend(chunk)
    return {
        "status": status,
        "url": url,
        "content_type": content_type,
        "location": location,
        "text": payload.decode(_charset(content_type), errors="replace"),
        "truncated": truncated,
    }


def _headers(raw: Any) -> dict[str, str]:
    headers = dict(DEFAULT_HEADERS)
    if not isinstance(raw, dict):
        return headers
    for name, value in raw.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise ToolError("headers must map strings to strings")
        key = name.strip().lower()
        if key in FORBIDDEN_HEADERS:
            raise ToolError(f"Header '{name}' must not be set by tools")
        if "\n" in value or "\r" in value:
            raise ToolError(f"Header '{name}' must not contain newlines")
        headers[key] = value
    return headers


def _charset(content_type: str) -> str:
    for part in content_type.split(";")[1:]:
        key, _, value = part.strip().partition("=")
        if key.strip().lower() == "charset" and value.strip():
            return value.strip().strip('"')
    return "utf-8"


__all__ = ["http_request_tool"]
