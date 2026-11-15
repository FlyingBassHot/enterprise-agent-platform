from __future__ import annotations

import os
import tempfile

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

_TEST_DIR = tempfile.mkdtemp(prefix="agent-platform-tests-")
os.environ["APP_DATABASE_URL"] = f"sqlite+aiosqlite:///{_TEST_DIR}/test.db"
os.environ["APP_STORAGE_DIR"] = f"{_TEST_DIR}/files"
os.environ["APP_ENV"] = "test"
os.environ["APP_AUTO_CREATE_TABLES"] = "true"
os.environ["APP_RATE_LIMIT_ENABLED"] = "false"

from app.main import app  # noqa: E402

ADMIN = {"username": "admin", "password": "admin-pass-123", "display_name": "Admin"}


async def admin_headers(client: AsyncClient) -> dict[str, str]:
    response = await client.post("/api/v1/auth/bootstrap", json=ADMIN)
    if response.status_code == 200:
        return {"Authorization": f"Bearer {response.json()['access_token']}"}
    response = await client.post(
        "/api/v1/auth/login",
        json={"username": ADMIN["username"], "password": ADMIN["password"]},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture
async def client() -> AsyncClient:
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as http:
            yield http


@pytest.fixture
async def admin(client: AsyncClient) -> dict[str, str]:
    return await admin_headers(client)


@pytest.fixture
async def db():
    from app.infrastructure.database.session import get_session_factory

    async with get_session_factory()() as session:
        yield session
        await session.rollback()
