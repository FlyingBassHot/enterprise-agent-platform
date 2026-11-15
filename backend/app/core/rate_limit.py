from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core.config import get_settings
from app.core.security import decode_access_token


@dataclass(slots=True)
class SlidingWindowLimiter:
    """Small in-process limiter for a single-worker deployment.

    Redis-backed limiting is a production replacement point; the interface is
    intentionally tiny so it can be swapped without touching routers.
    """

    window_seconds: float = 60.0
    _hits: dict[str, deque[float]] = field(default_factory=dict)

    def allow(self, key: str, limit: int, now: float | None = None) -> tuple[bool, int]:
        if limit <= 0:
            return True, 0
        current = now if now is not None else time.monotonic()
        if len(self._hits) > 5000:
            cutoff = current - self.window_seconds
            for old_key, old_bucket in list(self._hits.items()):
                if not old_bucket or old_bucket[-1] < cutoff:
                    self._hits.pop(old_key, None)
        bucket = self._hits.setdefault(key, deque())
        cutoff = current - self.window_seconds
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= limit:
            retry_after = max(1, int(self.window_seconds - (current - bucket[0])))
            return False, retry_after
        bucket.append(current)
        return True, 0

    def reset(self) -> None:
        self._hits.clear()


_limiter = SlidingWindowLimiter()


def get_limiter() -> SlidingWindowLimiter:
    return _limiter


def _subject(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    try:
        payload = decode_access_token(header[7:].strip())
    except Exception:  # noqa: BLE001 - invalid tokens are rejected by auth dependencies
        return None
    return str(payload.get("sub") or "") or None


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any):
        settings = get_settings()
        if not settings.rate_limit_enabled or request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
            return await call_next(request)

        path = request.url.path
        ip = request.client.host if request.client else "unknown"
        limiter = get_limiter()
        limit = 0
        key = ""
        if path == "/api/v1/auth/login" or path == "/api/v1/auth/bootstrap":
            limit = settings.rate_limit_login_per_minute
            key = f"login:{ip}"
        elif path == "/api/v1/executions" or path.endswith("/execute"):
            limit = settings.rate_limit_execution_per_minute
            key = f"execution:{_subject(request) or ip}"
        elif path == "/api/v1/artifacts" or (
            path.startswith("/api/v1/knowledge-bases/") and path.endswith("/documents")
        ):
            limit = settings.rate_limit_upload_per_minute
            key = f"upload:{_subject(request) or ip}"

        if limit:
            allowed, retry_after = limiter.allow(key, limit)
            if not allowed:
                return JSONResponse(
                    status_code=429,
                    headers={"Retry-After": str(retry_after)},
                    content={
                        "code": "rate_limited",
                        "message": "Too many requests; retry later",
                        "details": {"retry_after": retry_after},
                    },
                )
        return await call_next(request)


__all__ = ["RateLimitMiddleware", "SlidingWindowLimiter", "get_limiter"]
