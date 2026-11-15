from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.dependencies import get_current_user
from app.core.contracts import UserContext
from app.core.exceptions import (
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
    register_exception_handlers,
)
from app.infrastructure.database.base import Base
from app.infrastructure.database.session import get_engine, get_session_factory
from app.modules.audit_log.models import AuditLog
from app.modules.audit_log.router import router as audit_router
from app.modules.audit_log.schemas import AuditLogOut
from app.modules.audit_log.service import MAX_DETAILS_CHARS, MAX_LIMIT, AuditService

ADMIN = UserContext(user_id="admin-1", username="admin", is_superuser=True)
DEVELOPER = UserContext(
    user_id="dev-1",
    username="developer",
    roles=["developer"],
    permissions={"agents:read", "agents:write", "audit:read"},
)
VIEWER = UserContext(user_id="viewer-1", username="viewer", roles=["viewer"])


@pytest.fixture(autouse=True)
async def _schema() -> Any:
    """Ensure ``audit_logs`` exists even when a test does not start the app."""
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


def _audit_app(actor: UserContext) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(audit_router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: actor
    return app


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ---------------------------------------------------------------------------
# record()
# ---------------------------------------------------------------------------


async def test_record_persists_full_entry(db: Any) -> None:
    out = await AuditService(db).record(
        actor_id="user-1",
        actor_username="alice",
        action="http.POST /api/v1/agents",
        resource_type="agent",
        resource_id="agent-1",
        result="success",
        status_code=201,
        ip_address="10.0.0.7",
        request_id="req-123",
        tenant_id="acme",
        details={"method": "POST", "path": "/api/v1/agents"},
    )

    assert out.id
    assert out.action == "http.POST /api/v1/agents"
    assert out.actor_id == "user-1"
    assert out.actor_username == "alice"
    assert out.resource_type == "agent"
    assert out.resource_id == "agent-1"
    assert out.result == "success"
    assert out.status_code == 201
    assert out.ip_address == "10.0.0.7"
    assert out.request_id == "req-123"
    assert out.tenant_id == "acme"
    assert out.details == {"method": "POST", "path": "/api/v1/agents"}
    assert out.occurred_at.tzinfo is UTC
    assert out.created_at.tzinfo is UTC

    async with get_session_factory()() as fresh:
        row = await fresh.get(AuditLog, out.id)
        assert row is not None
        assert row.actor_username == "alice"
        assert row.status_code == 201
        assert row.details == {"method": "POST", "path": "/api/v1/agents"}


async def test_record_defaults(db: Any) -> None:
    out = await AuditService(db).record(action="auth.login")

    assert out.actor_id is None
    assert out.actor_username == ""
    assert out.resource_type == ""
    assert out.resource_id is None
    assert out.result == "success"
    assert out.status_code is None
    assert out.tenant_id == "default"
    assert out.details == {}


async def test_record_rejects_invalid_input(db: Any) -> None:
    service = AuditService(db)
    with pytest.raises(ValidationError):
        await service.record(action="")
    with pytest.raises(ValidationError):
        await service.record(action="   ")
    with pytest.raises(ValidationError):
        await service.record(action="auth.login", result="maybe")


# ---------------------------------------------------------------------------
# Masking and size cap
# ---------------------------------------------------------------------------


async def test_record_masks_secret_keys_recursively(db: Any) -> None:
    details = {
        "username": "alice",
        "password": "hunter2",
        "nested": {
            "access_token": "tok-abc",
            "api-key": "key-abc",
            "Authorization": "Bearer xyz",
            "secret": "shh",
            "key": "raw-key",
            "keep": 1,
        },
        "items": [{"token": "t-1"}, {"ok": True}],
    }
    out = await AuditService(db).record(
        action="test.masking",
        actor_username="alice",
        details=details,
    )

    assert out.details["username"] == "alice"
    assert out.details["password"] == "***"
    nested = out.details["nested"]
    assert nested["access_token"] == "***"
    assert nested["api-key"] == "***"
    assert nested["Authorization"] == "***"
    assert nested["secret"] == "***"
    assert nested["key"] == "***"
    assert nested["keep"] == 1
    assert out.details["items"] == [{"token": "***"}, {"ok": True}]

    serialized = json.dumps(out.details)
    for leaked in ("hunter2", "tok-abc", "key-abc", "Bearer xyz", "shh", "raw-key", "t-1"):
        assert leaked not in serialized

    # Masking happens before persistence, not just in the response.
    async with get_session_factory()() as fresh:
        row = await fresh.get(AuditLog, out.id)
        assert row is not None
        assert row.details["password"] == "***"
        assert row.details["nested"]["api-key"] == "***"


async def test_record_caps_details_size(db: Any) -> None:
    service = AuditService(db)
    bulky = {f"field_{index:03d}": "x" * 300 for index in range(40)}
    bulky["password"] = "hunter2"
    out = await service.record(action="test.size_cap", details=bulky)

    stored = json.dumps(out.details, ensure_ascii=False, sort_keys=True)
    assert len(stored) <= MAX_DETAILS_CHARS
    assert out.details["_truncated"] is True
    assert "hunter2" not in stored

    single = await service.record(action="test.size_cap_single", details={"blob": "y" * 10_000})
    assert single.details == {"_truncated": True}
    assert len(json.dumps(single.details)) <= MAX_DETAILS_CHARS


# ---------------------------------------------------------------------------
# Transaction control
# ---------------------------------------------------------------------------


async def test_commit_false_participates_in_caller_transaction(db: Any) -> None:
    service = AuditService(db)
    pending = await service.record(action="test.commit_false_rollback", commit=False)

    visible = await db.scalar(select(AuditLog).where(AuditLog.id == pending.id))
    assert visible is not None

    await db.rollback()
    async with get_session_factory()() as fresh:
        assert await fresh.get(AuditLog, pending.id) is None

    kept = await service.record(action="test.commit_false_commit", commit=False)
    await db.commit()
    async with get_session_factory()() as fresh:
        row = await fresh.get(AuditLog, kept.id)
        assert row is not None
        assert row.action == "test.commit_false_commit"


# ---------------------------------------------------------------------------
# list_logs() / get_log()
# ---------------------------------------------------------------------------


async def test_list_logs_filters_and_pagination(db: Any) -> None:
    base = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    scope = "test.paging"
    for index in range(5):
        db.add(
            AuditLog(
                occurred_at=base + timedelta(minutes=index),
                action=scope,
                actor_id="actor-a",
                actor_username="a",
                resource_type="agent",
                resource_id=f"agent-{index}",
                result="success",
                details={"index": index},
            )
        )
    db.add(
        AuditLog(
            occurred_at=base + timedelta(minutes=10),
            action=scope,
            actor_id="actor-b",
            actor_username="b",
            resource_type="tool",
            resource_id="tool-1",
            result="failure",
            details={},
        )
    )
    await db.commit()
    service = AuditService(db)

    newest = await service.list_logs(ADMIN, action=scope, limit=2)
    assert [row.resource_id for row in newest] == ["tool-1", "agent-4"]
    assert [row.occurred_at for row in newest] == [
        base + timedelta(minutes=10),
        base + timedelta(minutes=4),
    ]

    page_two = await service.list_logs(ADMIN, action=scope, limit=2, offset=2)
    assert [row.resource_id for row in page_two] == ["agent-3", "agent-2"]

    by_actor = await service.list_logs(ADMIN, action=scope, actor_id="actor-a")
    assert [row.resource_id for row in by_actor] == [
        "agent-4",
        "agent-3",
        "agent-2",
        "agent-1",
        "agent-0",
    ]

    by_resource_and_result = await service.list_logs(
        ADMIN, action=scope, resource_type="tool", result="failure"
    )
    assert [row.resource_id for row in by_resource_and_result] == ["tool-1"]

    window = await service.list_logs(
        ADMIN,
        action=scope,
        after=base + timedelta(minutes=1),
        before=base + timedelta(minutes=3),
    )
    assert [row.resource_id for row in window] == ["agent-3", "agent-2", "agent-1"]

    unknown = await service.list_logs(ADMIN, action="test.paging.missing")
    assert unknown == []


async def test_list_logs_clamps_limit(db: Any) -> None:
    base = datetime(2026, 4, 1, 0, 0, tzinfo=UTC)
    scope = "test.limit_clamp"
    db.add_all(
        [
            AuditLog(
                occurred_at=base + timedelta(seconds=index),
                action=scope,
                actor_username="bulk",
                details={},
            )
            for index in range(MAX_LIMIT + 25)
        ]
    )
    await db.commit()

    rows = await AuditService(db).list_logs(ADMIN, action=scope, limit=10_000)
    assert len(rows) == MAX_LIMIT
    # Newest first: the overflow rows are the oldest ones.
    assert rows[0].occurred_at == base + timedelta(seconds=MAX_LIMIT + 24)

    capped_offset = await AuditService(db).list_logs(ADMIN, action=scope, offset=-5, limit=1)
    assert len(capped_offset) == 1


async def test_get_log_and_not_found(db: Any) -> None:
    out = await AuditService(db).record(action="test.get_log")
    found = await AuditService(db).get_log(ADMIN, out.id)
    assert found.id == out.id
    assert found.action == "test.get_log"

    with pytest.raises(NotFoundError):
        await AuditService(db).get_log(ADMIN, "does-not-exist")


async def test_read_requires_audit_logs_permission(db: Any) -> None:
    out = await AuditService(db).record(action="test.permissions")
    service = AuditService(db)

    for actor in (VIEWER, DEVELOPER):
        with pytest.raises(PermissionDeniedError):
            await service.list_logs(actor)
        with pytest.raises(PermissionDeniedError):
            await service.get_log(actor, out.id)

    assert (await service.get_log(ADMIN, out.id)).id == out.id


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


async def test_api_list_and_get_masks_secrets(db: Any) -> None:
    seeded = await AuditService(db).record(
        action="http.POST /api/v1/agents",
        actor_id="admin-1",
        actor_username="admin",
        resource_type="agent",
        resource_id="agent-1",
        status_code=201,
        ip_address="127.0.0.1",
        request_id="req-api",
        details={
            "password": "hunter2",
            "nested": {"api_key": "key-abc"},
            "name": "planner",
        },
    )

    async with _client(_audit_app(ADMIN)) as http:
        response = await http.get(
            "/api/v1/audit-logs",
            params={"action": "http.POST /api/v1/agents", "resource_type": "agent"},
        )
        assert response.status_code == 200, response.text
        items = response.json()
        item = next(entry for entry in items if entry["id"] == seeded.id)
        assert item["details"]["password"] == "***"
        assert item["details"]["nested"]["api_key"] == "***"
        assert item["details"]["name"] == "planner"
        assert "hunter2" not in response.text
        assert item["occurred_at"].endswith(("Z", "+00:00"))

        single = await http.get(f"/api/v1/audit-logs/{seeded.id}")
        assert single.status_code == 200
        assert single.json()["id"] == seeded.id
        assert "hunter2" not in single.text

        missing = await http.get("/api/v1/audit-logs/missing-id")
        assert missing.status_code == 404
        assert missing.json()["code"] == "not_found"

        assert (await http.get("/api/v1/audit-logs", params={"limit": 501})).status_code == 422
        assert (await http.get("/api/v1/audit-logs", params={"limit": 0})).status_code == 422
        assert (await http.get("/api/v1/audit-logs", params={"offset": -1})).status_code == 422
        assert (await http.get("/api/v1/audit-logs", params={"result": "maybe"})).status_code == 422


async def test_api_forbidden_for_developer_and_viewer(db: Any) -> None:
    seeded = await AuditService(db).record(action="test.api_forbidden")

    for actor in (DEVELOPER, VIEWER):
        async with _client(_audit_app(actor)) as http:
            listed = await http.get("/api/v1/audit-logs")
            assert listed.status_code == 403
            assert listed.json()["code"] == "permission_denied"

            single = await http.get(f"/api/v1/audit-logs/{seeded.id}")
            assert single.status_code == 403
            assert single.json()["code"] == "permission_denied"


async def test_router_is_append_only() -> None:
    methods = {
        method for route in audit_router.routes for method in getattr(route, "methods", set())
    }
    assert methods == {"GET"}
    assert {route.path for route in audit_router.routes} == {
        "/audit-logs",
        "/audit-logs/{log_id}",
    }

    index_names = {index.name for index in AuditLog.__table__.indexes}
    assert {
        "ix_audit_logs_occurred_at",
        "ix_audit_logs_actor_id",
        "ix_audit_logs_action",
        "ix_audit_logs_occurred_at_action_actor_id",
    } <= index_names

    out = AuditLogOut.model_validate(
        {
            "id": "log-1",
            "occurred_at": datetime(2026, 1, 1, tzinfo=UTC),
            "action": "auth.login",
            "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        }
    )
    assert out.result == "success"
    assert out.details == {}
