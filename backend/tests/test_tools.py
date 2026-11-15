from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any

import httpx
import pytest
from httpx import AsyncClient
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import delete, func, select

from app.core.contracts import ToolContext, ToolDescriptor, ToolInvocation, UserContext
from app.core.exceptions import ValidationError
from app.infrastructure.database.session import init_db
from app.modules.tool.adapters.http import execute_http_tool
from app.modules.tool.exceptions import ToolDefinitionError
from app.modules.tool.models import Tool, ToolExecution, ToolPermission, ToolVersion
from app.modules.tool.registry import NativeToolRegistry, native_registry
from app.modules.tool.schemas import ToolCreate, ToolUpdate
from app.modules.tool.service import ToolService

CALL_LOG: list[str] = []

TOOL_PERMISSIONS = {"tools:read", "tools:write", "tools:execute"}


def _actor(
    *extra: str, roles: Sequence[str] = (), superuser: bool = False
) -> UserContext:
    return UserContext(
        user_id="test-user",
        username="tester",
        display_name="Tester",
        is_superuser=superuser,
        roles=list(roles),
        permissions=set(TOOL_PERMISSIONS) | set(extra),
    )


ECHO_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "password": {"type": "string"},
        "profile": {
            "type": "object",
            "properties": {"password": {"type": "string"}},
        },
    },
    "required": ["text"],
}


async def _echo_handler(arguments: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return {"echo": arguments["text"], "user": ctx.user.username, "tool": ctx.tool_name}


async def _slow_handler(arguments: dict[str, Any], ctx: ToolContext) -> str:
    CALL_LOG.append("slow")
    await asyncio.sleep(2)
    return "late"


async def _fail_handler(arguments: dict[str, Any], ctx: ToolContext) -> str:
    CALL_LOG.append("fail")
    token = arguments.get("token")
    raise RuntimeError(f"failed with token={token}" if token else "boom")


async def _high_risk_handler(arguments: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    CALL_LOG.append("high")
    return {"approved_action": arguments.get("action", "")}


async def _secure_handler(arguments: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return {"secured": True}


def _register_native_tools() -> None:
    native_registry.register(
        ToolDescriptor(
            name="test_tool_echo",
            description="Echo text",
            input_schema=ECHO_SCHEMA,
            risk_level="low",
            timeout_seconds=10,
        ),
        _echo_handler,
    )
    native_registry.register(
        ToolDescriptor(
            name="test_tool_slow",
            description="Slow tool",
            input_schema={"type": "object", "properties": {}},
            timeout_seconds=1,
        ),
        _slow_handler,
    )
    native_registry.register(
        ToolDescriptor(
            name="test_tool_fail",
            description="Failing tool",
            input_schema={"type": "object", "properties": {}},
        ),
        _fail_handler,
    )
    native_registry.register(
        ToolDescriptor(
            name="test_tool_high",
            description="High risk tool",
            input_schema={"type": "object", "properties": {"action": {"type": "string"}}},
            risk_level="high",
            requires_approval=True,
        ),
        _high_risk_handler,
    )
    native_registry.register(
        ToolDescriptor(
            name="test_tool_secure",
            description="Scoped tool",
            permission_scope=["audit:read"],
        ),
        _secure_handler,
    )


@pytest.fixture(autouse=True)
def isolate_native_registry():
    CALL_LOG.clear()
    native_registry.clear()
    yield
    native_registry.clear()


@pytest.fixture
async def clean_db(db):
    await init_db()
    await db.execute(delete(ToolExecution))
    await db.execute(delete(ToolPermission))
    await db.execute(delete(ToolVersion))
    await db.execute(delete(Tool))
    await db.commit()
    return db


@pytest.fixture
def registered_tools() -> None:
    _register_native_tools()


@pytest.fixture
async def service(clean_db, registered_tools) -> ToolService:
    tool_service = ToolService(clean_db)
    await tool_service.sync_native()
    return tool_service


async def _tool_row(db, name: str) -> Tool:
    tool = await db.scalar(select(Tool).where(Tool.name == name))
    assert tool is not None
    return tool


async def test_registry_rejects_invalid_schemas() -> None:
    registry = NativeToolRegistry()
    with pytest.raises(ToolDefinitionError):
        registry.register(
            ToolDescriptor(name="bad", input_schema={"type": "array"}), _echo_handler
        )
    with pytest.raises(ToolDefinitionError):
        registry.register(
            ToolDescriptor(name="bad2", input_schema={"type": "object", "properties": 1}),
            _echo_handler,
        )
    with pytest.raises(ToolDefinitionError):
        registry.register(ToolDescriptor(name=""), _echo_handler)


async def test_sync_native_creates_and_versions(service: ToolService, clean_db) -> None:
    actor = _actor(superuser=True)
    tools = await service.list_tools(actor)
    names = {tool.name for tool in tools}
    assert {
        "test_tool_echo",
        "test_tool_slow",
        "test_tool_fail",
        "test_tool_high",
        "test_tool_secure",
    } <= names

    echo = await service.get_tool("test_tool_echo")
    assert echo.source_type == "native"
    assert echo.current_version == 1
    assert echo.permission_scope == []

    await service.sync_native()
    assert (await service.get_tool("test_tool_echo")).current_version == 1

    await service.set_enabled(echo.id, False, actor)
    native_registry.register(
        ToolDescriptor(
            name="test_tool_echo",
            description="Echo text v2",
            input_schema=ECHO_SCHEMA,
            risk_level="low",
            timeout_seconds=10,
        ),
        _echo_handler,
    )
    await service.sync_native()

    refreshed = await service.get_tool("test_tool_echo")
    assert refreshed.current_version == 2
    assert refreshed.description == "Echo text v2"
    assert refreshed.enabled is False
    version_count = await clean_db.scalar(
        select(func.count())
        .select_from(ToolVersion)
        .where(ToolVersion.tool_id == echo.id)
    )
    assert version_count == 2

    await service.sync_native()
    assert (await service.get_tool("test_tool_echo")).current_version == 2


async def test_describe_filters_and_validates(service: ToolService, clean_db) -> None:
    admin = _actor(superuser=True)
    descriptors = await service.describe(["test_tool_echo", "test_tool_high"], admin)
    assert [item.name for item in descriptors] == ["test_tool_echo", "test_tool_high"]
    assert all(item.enabled for item in descriptors)
    assert await service.describe([], admin) == []

    with pytest.raises(ValidationError) as excinfo:
        await service.describe(["test_tool_echo", "test_tool_missing"], admin)
    assert excinfo.value.details == {"missing": ["test_tool_missing"]}

    plain = _actor()
    assert await service.describe(["test_tool_secure"], plain) == []
    assert len(await service.describe(["test_tool_secure"], _actor("audit:read"))) == 1

    echo = await _tool_row(clean_db, "test_tool_echo")
    await service.set_enabled(echo.id, False, admin)
    with pytest.raises(ValidationError) as excinfo:
        await service.describe(["test_tool_echo"], admin)
    assert excinfo.value.details == {"missing": ["test_tool_echo"]}


async def test_execute_schema_validation_records_failure(
    service: ToolService, clean_db
) -> None:
    actor = _actor(superuser=True)
    result = await service.execute(
        ToolInvocation(
            tool_name="test_tool_echo",
            arguments={"password": "ignored"},
            user_id=actor.user_id,
            execution_id="exec-invalid",
        ),
        actor,
    )
    assert result.ok is False
    assert result.status == "failed"
    assert result.error is not None and "text" in result.error

    record = await clean_db.scalar(
        select(ToolExecution).where(ToolExecution.execution_id == "exec-invalid")
    )
    assert record is not None
    assert record.status == "failed"
    assert record.tool_name == "test_tool_echo"
    assert CALL_LOG == []


async def test_permission_scope_denial_grant_and_superuser(
    service: ToolService, clean_db
) -> None:
    plain = _actor()
    denied = await service.execute(
        ToolInvocation(
            tool_name="test_tool_secure", arguments={}, user_id=plain.user_id
        ),
        plain,
    )
    assert denied.status == "denied"
    assert denied.error == "permission_denied"

    authorized = _actor("audit:read")
    ok = await service.execute(
        ToolInvocation(
            tool_name="test_tool_secure", arguments={}, user_id=authorized.user_id
        ),
        authorized,
    )
    assert ok.ok and ok.output == {"secured": True}

    secure = await _tool_row(clean_db, "test_tool_secure")
    clean_db.add(
        ToolPermission(
            tool_id=secure.id,
            subject_type="role",
            subject_id="auditor",
            actions=["execute"],
        )
    )
    await clean_db.commit()
    grantee = _actor(roles=["auditor"])
    granted = await service.execute(
        ToolInvocation(
            tool_name="test_tool_secure", arguments={}, user_id=grantee.user_id
        ),
        grantee,
    )
    assert granted.ok is True

    superuser_result = await service.execute(
        ToolInvocation(
            tool_name="test_tool_secure",
            arguments={},
            user_id="root",
        ),
        _actor(superuser=True),
    )
    assert superuser_result.ok is True

    statuses = list(
        (
            await clean_db.scalars(
                select(ToolExecution.status).where(
                    ToolExecution.tool_name == "test_tool_secure"
                )
            )
        ).all()
    )
    assert statuses.count("denied") == 1
    assert statuses.count("success") == 3


async def test_high_risk_requires_approval(service: ToolService, clean_db) -> None:
    actor = _actor(superuser=True)
    denied = await service.execute(
        ToolInvocation(
            tool_name="test_tool_high",
            arguments={"action": "delete"},
            user_id=actor.user_id,
            execution_id="exec-high-denied",
        ),
        actor,
    )
    assert denied.ok is False
    assert denied.status == "denied"
    assert denied.error == "approval_required"
    assert CALL_LOG == []

    approved = await service.execute(
        ToolInvocation(
            tool_name="test_tool_high",
            arguments={"action": "delete"},
            user_id=actor.user_id,
            execution_id="exec-high-approved",
            approved=True,
        ),
        actor,
    )
    assert approved.ok is True
    assert approved.output == {"approved_action": "delete"}
    assert CALL_LOG == ["high"]

    denied_record = await clean_db.scalar(
        select(ToolExecution).where(ToolExecution.execution_id == "exec-high-denied")
    )
    approved_record = await clean_db.scalar(
        select(ToolExecution).where(ToolExecution.execution_id == "exec-high-approved")
    )
    assert denied_record is not None
    assert (denied_record.status, denied_record.error) == ("denied", "approval_required")
    assert approved_record is not None
    assert (approved_record.status, approved_record.error) == ("success", None)


async def test_timeout_and_failure_records(service: ToolService, clean_db) -> None:
    actor = _actor(superuser=True)
    timed_out = await service.execute(
        ToolInvocation(
            tool_name="test_tool_slow",
            arguments={},
            user_id=actor.user_id,
            execution_id="exec-timeout",
        ),
        actor,
    )
    assert timed_out.status == "timeout"
    assert timed_out.error is not None and "timed out" in timed_out.error

    failed = await service.execute(
        ToolInvocation(
            tool_name="test_tool_fail",
            arguments={},
            user_id=actor.user_id,
            execution_id="exec-fail",
        ),
        actor,
    )
    assert failed.status == "failed"
    assert failed.error == "RuntimeError: boom"

    timeout_record = await clean_db.scalar(
        select(ToolExecution).where(ToolExecution.execution_id == "exec-timeout")
    )
    fail_record = await clean_db.scalar(
        select(ToolExecution).where(ToolExecution.execution_id == "exec-fail")
    )
    assert timeout_record is not None and timeout_record.status == "timeout"
    assert fail_record is not None and fail_record.status == "failed"


async def test_sensitive_masking_and_error_scrubbing(service: ToolService, clean_db) -> None:
    actor = _actor(superuser=True)
    echo = await _tool_row(clean_db, "test_tool_echo")
    await service.update_tool(echo.id, ToolUpdate(sensitive_fields=["password"]), actor)

    result = await service.execute(
        ToolInvocation(
            tool_name="test_tool_echo",
            arguments={
                "text": "hi",
                "password": "s3cret",
                "profile": {"password": "deep-secret"},
            },
            user_id=actor.user_id,
            execution_id="exec-mask",
        ),
        actor,
    )
    assert result.ok is True
    assert result.masked_arguments == {
        "text": "hi",
        "password": "***",
        "profile": {"password": "***"},
    }
    record = await clean_db.scalar(
        select(ToolExecution).where(ToolExecution.execution_id == "exec-mask")
    )
    assert record is not None
    serialized = json.dumps(record.arguments_masked)
    assert "s3cret" not in serialized
    assert "deep-secret" not in serialized

    fail_tool = await _tool_row(clean_db, "test_tool_fail")
    await service.update_tool(fail_tool.id, ToolUpdate(sensitive_fields=["token"]), actor)
    errored = await service.execute(
        ToolInvocation(
            tool_name="test_tool_fail",
            arguments={"token": "topsecret"},
            user_id=actor.user_id,
            execution_id="exec-scrub",
        ),
        actor,
    )
    assert errored.status == "failed"
    assert errored.error is not None
    assert "topsecret" not in errored.error
    assert "***" in errored.error


async def test_subset_mocked_transport_and_masking(clean_db) -> None:
    received: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        received["method"] = request.method
        received["url"] = str(request.url)
        received["authorization"] = request.headers.get("authorization")
        received["body"] = json.loads(request.content)
        return httpx.Response(200, json={"received": True})

    transport = httpx.MockTransport(handler)
    tool_service = ToolService(clean_db, http_transport=transport)
    created = await tool_service.create_tool(
        ToolCreate(
            name="test_tool_http_echo",
            description="HTTP echo",
            source_type="http",
            input_schema={
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                    "secret": {"type": "string"},
                },
            },
            sensitive_fields=["secret"],
            config={
                "method": "POST",
                "url": "https://api.example.test/echo",
                "headers": {"Authorization": "Bearer ${TOOL_SECRET_TEST_TOOL_TOKEN}"},
                "query": {"q": "1"},
                "body_mode": "json",
            },
        ),
        _actor(superuser=True),
    )
    assert created.current_version == 1

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("TOOL_SECRET_TEST_TOOL_TOKEN", "token-123")
    try:
        actor = _actor(superuser=True)
        result = await tool_service.execute(
            ToolInvocation(
                tool_name="test_tool_http_echo",
                arguments={"message": "hello", "secret": "hidden-value"},
                user_id=actor.user_id,
                execution_id="exec-http",
            ),
            actor,
        )
        assert result.ok is True
        assert result.output == {"received": True}
        assert received["method"] == "POST"
        assert "q=1" in received["url"]
        assert received["authorization"] == "Bearer token-123"
        assert received["body"] == {"message": "hello", "secret": "hidden-value"}
        assert result.masked_arguments == {"message": "hello", "secret": "***"}

        record = await clean_db.scalar(
            select(ToolExecution).where(ToolExecution.execution_id == "exec-http")
        )
        assert record is not None
        assert "hidden-value" not in json.dumps(record.arguments_masked)
        assert record.result == {"received": True}

        monkeypatch.delenv("TOOL_SECRET_TEST_TOOL_TOKEN")
        missing_env = await tool_service.execute(
            ToolInvocation(
                tool_name="test_tool_http_echo",
                arguments={"message": "hello"},
                user_id=actor.user_id,
                execution_id="exec-http-env",
            ),
            actor,
        )
        assert missing_env.status == "failed"
        assert missing_env.error is not None
        assert "TOOL_SECRET_TEST_TOOL_TOKEN" in missing_env.error
        assert "hidden-value" not in missing_env.error

        def failing(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="kaboom")

        monkeypatch.setenv("TOOL_SECRET_TEST_TOOL_TOKEN", "token-123")
        broken = ToolService(clean_db, http_transport=httpx.MockTransport(failing))
        server_error = await broken.execute(
            ToolInvocation(
                tool_name="test_tool_http_echo",
                arguments={"message": "hello"},
                user_id=actor.user_id,
                execution_id="exec-http-500",
            ),
            actor,
        )
        assert server_error.status == "failed"
        assert server_error.error is not None
        assert "500" in server_error.error and "kaboom" in server_error.error
    finally:
        monkeypatch.undo()


async def test_http_update_versions_and_masking(clean_db) -> None:
    received: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        received["query"] = str(request.url)
        received["body"] = request.content.decode()
        return httpx.Response(200, json={"ok": True})

    tool_service = ToolService(clean_db, http_transport=httpx.MockTransport(handler))
    actor = _actor(superuser=True)
    created = await tool_service.create_tool(
        ToolCreate(
            name="test_tool_http_form",
            source_type="http",
            config={
                "method": "POST",
                "url": "https://api.example.test/form",
                "body_mode": "form",
                "query": {"fixed": "yes"},
            },
        ),
        actor,
    )
    updated = await tool_service.update_tool(
        created.id, ToolUpdate(description="form v2"), actor
    )
    assert updated.current_version == 1
    renamed = await tool_service.update_tool(
        created.id, ToolUpdate(display_name="Form v2"), actor
    )
    assert renamed.current_version == 1
    changed = await tool_service.update_tool(
        created.id,
        ToolUpdate(config={"method": "POST", "url": "https://api.example.test/form-v2", "body_mode": "form", "query": {"fixed": "yes"}}),
        actor,
    )
    assert changed.current_version == 2

    result = await tool_service.execute(
        ToolInvocation(
            tool_name="test_tool_http_form",
            arguments={"name": "Ada", "count": 3},
            user_id=actor.user_id,
        ),
        actor,
    )
    assert result.ok is True
    assert "fixed=yes" in received["query"]
    assert "name=Ada" in received["body"] and "count=3" in received["body"]


async def test_service_test_tool_cannot_approve_high_risk(service: ToolService) -> None:
    actor = _actor(superuser=True)
    result = await service.test_tool("test_tool_high", {"action": "go"}, actor)
    assert result.ok is False
    assert result.status == "denied"
    assert result.error == "high_risk_test_requires_approval"
    assert CALL_LOG == []

    ok = await service.test_tool("test_tool_echo", {"text": "hi"}, actor)
    assert ok.ok is True
    assert ok.result is not None and ok.result["echo"] == "hi"


async def test_http_config_validation() -> None:
    with pytest.raises(PydanticValidationError):
        ToolCreate(
            name="test_tool_bad_http",
            source_type="http",
            config={
                "method": "GET",
                "url": "https://api.example.test/x",
                "headers": {"Authorization": "Bearer raw-secret-value"},
            },
        )
    with pytest.raises(PydanticValidationError):
        ToolCreate(
            name="test_tool_bad_env_prefix",
            source_type="http",
            config={
                "method": "GET",
                "url": "https://api.example.test/x",
                "headers": {"Authorization": "Bearer ${APP_SECRET_KEY}"},
            },
        )
    with pytest.raises(PydanticValidationError):
        ToolCreate(
            name="test_tool_bad_http2",
            source_type="http",
            config={"method": "GET", "url": "ftp://api.example.test/x"},
        )
    with pytest.raises(PydanticValidationError):
        ToolCreate(name="test_tool_bad_native", source_type="native", config=None, input_schema={"type": "array"})


async def test_http_adapter_rejects_non_http_url() -> None:
    with pytest.raises(ToolDefinitionError):
        await execute_http_tool({"method": "GET", "url": "file:///etc/passwd"}, {})


async def test_api_crud_and_test_endpoint(
    client: AsyncClient, admin: dict[str, str], clean_db, registered_tools: None
) -> None:
    created = await client.post(
        "/api/v1/tools",
        headers=admin,
        json={
            "name": "test_tool_echo",
            "source_type": "native",
            "display_name": "Echo",
            "sensitive_fields": ["password"],
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    tool_id = body["id"]
    assert body["source_type"] == "native"
    assert body["current_version"] == 1
    assert body["requires_approval"] is False

    listing = await client.get("/api/v1/tools", headers=admin)
    assert listing.status_code == 200
    assert any(item["id"] == tool_id for item in listing.json())

    detail = await client.get(f"/api/v1/tools/{tool_id}", headers=admin)
    assert detail.status_code == 200
    assert detail.json()["name"] == "test_tool_echo"

    updated = await client.put(
        f"/api/v1/tools/{tool_id}",
        headers=admin,
        json={"display_name": "Echo Renamed", "sensitive_fields": ["password", "secret"]},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["display_name"] == "Echo Renamed"
    assert updated.json()["current_version"] == 2

    tested = await client.post(
        f"/api/v1/tools/{tool_id}/test",
        headers=admin,
        json={"arguments": {"text": "from-api", "password": "do-not-log"}},
    )
    assert tested.status_code == 200, tested.text
    tested_body = tested.json()
    assert tested_body["ok"] is True
    assert tested_body["result"]["echo"] == "from-api"
    assert tested_body["masked_arguments"]["password"] == "***"

    disabled = await client.post(f"/api/v1/tools/{tool_id}/disable", headers=admin)
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    enabled = await client.post(f"/api/v1/tools/{tool_id}/enable", headers=admin)
    assert enabled.status_code == 200
    assert enabled.json()["enabled"] is True

    unknown = await client.post(
        "/api/v1/tools",
        headers=admin,
        json={"name": "test_tool_unregistered", "source_type": "native"},
    )
    assert unknown.status_code == 422
    assert unknown.json()["code"] == "tool_definition_error"

    literal_secret = await client.post(
        "/api/v1/tools",
        headers=admin,
        json={
            "name": "test_tool_http_bad",
            "source_type": "http",
            "config": {
                "method": "GET",
                "url": "https://api.example.test/x",
                "headers": {"X-Api-Key": "raw-secret-value"},
            },
        },
    )
    assert literal_secret.status_code == 422

    native_delete = await client.delete(f"/api/v1/tools/{tool_id}", headers=admin)
    assert native_delete.status_code == 422
    still_there = await client.get(f"/api/v1/tools/{tool_id}", headers=admin)
    assert still_there.status_code == 200


async def test_developer_cannot_create_http_tools(
    client: AsyncClient, admin: dict[str, str]
) -> None:
    await client.post(
        "/api/v1/users",
        headers=admin,
        json={
            "username": "tool-developer",
            "password": "developer-pass-123",
            "role_names": ["developer"],
        },
    )
    login = await client.post(
        "/api/v1/auth/login",
        json={"username": "tool-developer", "password": "developer-pass-123"},
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    response = await client.post(
        "/api/v1/tools",
        headers=headers,
        json={
            "name": "developer_http_tool",
            "source_type": "http",
            "config": {"method": "GET", "url": "https://api.example.test/x"},
        },
    )
    assert response.status_code == 403
