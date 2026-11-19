"""Optional public research APIs. No keys or public-host allowlist => unavailable."""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import os
import socket
import sqlite3
import ssl
import time
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.robotparser import RobotFileParser

from mcp.server.mcpserver import MCPServer

server = MCPServer("postgrad-research")
MAX_BYTES = 1024 * 1024
USER_AGENT = "PostgraduateResearch/1.0"


def _reserve(bucket: str, *, now: float | None = None) -> bool:
    """Persist quotas across the platform's short-lived stdio processes."""
    now = time.time() if now is None else now
    root = Path(os.getenv("POSTGRAD_DATA_DIR") or Path(__file__).resolve().parents[1] / "data")
    root.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(root / "research_requests.sqlite3", timeout=5)
    try:
        connection.execute("CREATE TABLE IF NOT EXISTS requests (bucket TEXT, ts REAL)")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM requests WHERE ts < ?", (now - 3600,))
        count, last = connection.execute(
            "SELECT COUNT(*), MAX(ts) FROM requests WHERE bucket = ?", (bucket,)
        ).fetchone()
        if count >= 30 or (last is not None and now - last < 2):
            connection.rollback()
            return False
        connection.execute("INSERT INTO requests VALUES (?, ?)", (bucket, now))
        connection.commit()
        return True
    finally:
        connection.close()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _public_addresses(host: str) -> list[str]:
    values = list(
        dict.fromkeys(r[4][0] for r in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM))
    )
    if not values or any(
        not ipaddress.ip_address(v).is_global or ipaddress.ip_address(v).is_multicast
        for v in values
    ):
        raise ValueError("Host resolves to a non-public address")
    return values


class _PinnedHTTPS(http.client.HTTPSConnection):
    def connect(self):
        addresses = _public_addresses(self.host)
        raw = socket.create_connection((addresses[0], 443), timeout=self.timeout)
        try:
            self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def _request(host: str, path: str, headers: dict | None = None) -> tuple[int, str, bytes]:
    connection = _PinnedHTTPS(host, timeout=10)
    try:
        connection.request(
            "GET",
            path,
            headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity", **(headers or {})},
        )
        response = connection.getresponse()
        if response.getheader("Content-Encoding", "identity") != "identity":
            raise ValueError("Compressed responses are not accepted")
        body = response.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise ValueError("Response exceeds 1 MiB")
        return response.status, response.getheader("Content-Type", ""), body
    finally:
        connection.close()


def _json_request(host: str, path: str, headers: dict | None = None) -> dict:
    status, _, body = _request(host, path, headers)
    if status != 200:
        raise ValueError(f"Upstream HTTP {status}")
    parsed = json.loads(body)
    if not isinstance(parsed, dict):
        raise ValueError("Upstream returned invalid JSON shape")
    return parsed


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"}:
            self.skip = max(0, self.skip - 1)

    def handle_data(self, data):
        if not self.skip and data.strip():
            self.parts.append(data.strip())


@server.tool()
def search_web(query: str, count: int = 5) -> dict:
    """Search Brave when administratively configured; snippets remain unverified."""
    key = os.getenv("BRAVE_API_KEY", "")
    if not key:
        return {"status": "unavailable", "reason": "BRAVE_API_KEY not configured", "results": []}
    if not query.strip() or len(query) > 1500 or not 1 <= count <= 10:
        raise ValueError("Query must be 1..1500 characters; count 1..10")
    try:
        if not _reserve("brave"):
            return {"status": "rate_limited", "results": []}
        raw = _json_request(
            "api.search.brave.com",
            "/res/v1/web/search?" + urlencode({"q": query, "count": count}),
            {"X-Subscription-Token": key},
        )
        rows = raw.get("web", {}).get("results", [])[:count]
        return {
            "status": "ok",
            "fetched_at": _now(),
            "verified": False,
            "trust": "untrusted_external_data",
            "results": [
                {
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "snippet": r.get("description", "")[:1200],
                }
                for r in rows
            ],
        }
    except Exception:
        return {
            "status": "unavailable",
            "reason": "Search request failed; check provider configuration",
            "results": [],
        }


@server.tool()
def fetch_public_page(url: str) -> dict:
    """Read a size-limited, allowlisted public HTTPS page, honoring robots.txt."""
    parts = urlsplit(url)
    hosts = {
        h.strip().lower() for h in os.getenv("POSTGRAD_PUBLIC_HOSTS", "").split(",") if h.strip()
    }
    if not hosts:
        return {"status": "unavailable", "reason": "POSTGRAD_PUBLIC_HOSTS not configured"}
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.hostname.lower() not in hosts
        or parts.username
        or parts.password
        or parts.port not in {None, 443}
        or len(url) > 2000
        or any(c in url for c in "\r\n\x00")
    ):
        raise ValueError("Only explicitly allowlisted public HTTPS hosts on port 443 are accepted")
    host = parts.hostname.lower()
    try:
        if not _reserve("public:" + host):
            return {"status": "rate_limited", "reason": "Per-host request quota reached"}
        code, _, robots = _request(host, "/robots.txt")
        if code == 200:
            parser = RobotFileParser()
            parser.parse(robots.decode("utf-8", errors="replace").splitlines())
            if not parser.can_fetch(USER_AGENT, url):
                return {"status": "denied", "reason": "robots.txt disallows this URL"}
            delay = parser.crawl_delay(USER_AGENT) or parser.crawl_delay("*") or 1
            if delay > 5:
                return {
                    "status": "unavailable",
                    "reason": "Site requires a crawl delay above this adapter limit",
                }
        elif code == 404:
            delay = 1
        else:
            return {"status": "unavailable", "reason": "Cannot establish robots.txt policy"}
        time.sleep(delay)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        code, content_type, body = _request(host, path)
        if code != 200:
            return {
                "status": "unavailable",
                "http_status": code,
                "reason": "Redirects are not followed; verify destination separately",
            }
        if not any(t in content_type.lower() for t in ("text/html", "text/plain")):
            return {
                "status": "unsupported",
                "reason": "Use the local documents MCP for PDF/Word; this endpoint accepts HTML/text",
            }
        text = body.decode("utf-8", errors="replace")
        if "html" in content_type.lower():
            parser = _Text()
            parser.feed(text)
            text = "\n".join(parser.parts)
        return {
            "status": "ok",
            "url": url,
            "fetched_at": _now(),
            "sha256": hashlib.sha256(body).hexdigest(),
            "trust": "untrusted_external_data",
            "verified": False,
            "text": text[:12000],
            "truncated": len(text) > 12000,
        }
    except Exception:
        return {
            "status": "unavailable",
            "reason": "Public fetch failed or was blocked by network policy",
        }


@server.tool()
def geocode_campus(university: str, campus: str, address: str, city: str = "") -> dict:
    """Geocode an explicitly supplied campus address through Amap; do not infer campus."""
    key = os.getenv("AMAP_API_KEY", "")
    if not key:
        return {"status": "unavailable", "reason": "AMAP_API_KEY not configured"}
    if not all(v.strip() for v in (university, campus, address)) or len(address) > 500:
        raise ValueError("University, campus and exact address are required")
    try:
        if not _reserve("amap"):
            return {"status": "rate_limited", "candidates": []}
        raw = _json_request(
            "restapi.amap.com",
            "/v3/geocode/geo?" + urlencode({"key": key, "address": address, "city": city}),
        )
        if raw.get("status") != "1":
            return {"status": "unavailable", "reason": "Amap declined the query"}
        return {
            "status": "ok",
            "university": university,
            "campus": campus,
            "fetched_at": _now(),
            "provider": "amap",
            "trust": "untrusted_external_data",
            "coordinate_system": "GCJ-02",
            "candidates": raw.get("geocodes", [])[:5],
            "warnings": [
                "Candidates require address verification; coordinates do not establish program campus"
            ],
        }
    except Exception:
        return {
            "status": "unavailable",
            "reason": "Map request failed; check provider configuration",
        }


if __name__ == "__main__":
    server.run("stdio")
