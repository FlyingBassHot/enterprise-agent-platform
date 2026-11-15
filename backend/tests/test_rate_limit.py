from __future__ import annotations

from types import SimpleNamespace

import httpx
from fastapi import FastAPI

from app.core.rate_limit import RateLimitMiddleware, get_limiter


async def test_sliding_window_limiter() -> None:
    from app.core.rate_limit import SlidingWindowLimiter

    limiter = SlidingWindowLimiter(window_seconds=60)
    assert limiter.allow("k", 2, now=0.0) == (True, 0)
    assert limiter.allow("k", 2, now=1.0) == (True, 0)
    allowed, retry = limiter.allow("k", 2, now=2.0)
    assert allowed is False and 58 <= retry <= 60
    assert limiter.allow("k", 2, now=61.1) == (True, 0)


async def test_login_rate_limit_middleware(monkeypatch) -> None:
    settings = SimpleNamespace(
        rate_limit_enabled=True,
        rate_limit_login_per_minute=2,
        rate_limit_execution_per_minute=10,
        rate_limit_upload_per_minute=10,
    )
    monkeypatch.setattr("app.core.rate_limit.get_settings", lambda: settings)
    get_limiter().reset()
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware)

    @app.post("/api/v1/auth/login")
    async def login():
        return {"ok": True}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.post("/api/v1/auth/login")).status_code == 200
        assert (await client.post("/api/v1/auth/login")).status_code == 200
        limited = await client.post("/api/v1/auth/login")
        assert limited.status_code == 429
        assert limited.json()["code"] == "rate_limited"
        assert "Retry-After" in limited.headers
    get_limiter().reset()
