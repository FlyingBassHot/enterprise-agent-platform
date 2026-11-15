from __future__ import annotations

import json
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from mcp import types
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select

from app.core.contracts import ToolInvocation, UserContext
from app.core.exceptions import PermissionDeniedError, ToolError
from app.infrastructure.database.session import init_db
from app.modules.tool.exceptions import ToolDefinitionError, ToolExecutionError
from app.modules.tool.mcp_client import MAX_RESULT_CHARS, normalize_call_result
from app.modules.tool.mcp_service import MCPService, synced_tool_name
from app.modules.tool.models import MCPServer, Tool
from app.modules.tool.schemas import MCPServerCreate, MCPServerUpdate, ToolCreate
from app.modules.tool.service import ToolService

FIXTURE_SERVER = Path(__file__).resolve().parent / "fixtures" / "mcp_echo_server.py"


def _admin() -> UserContext:
    return UserContext(
        user_id=str(uuid4()),
        username="mcp-admin",
        display_name="MCP Admin",
        is_superuser=True,
    )


def _server_name() -> str:
    return f"mcp-{uuid4().hex[:10]}"


def _stdio_payload(name: str, *, env: dict[str, str] | None = None) -> MCPServerCreate:
    return MCPServerCreate(
        name=name,
        transport="stdio",
        command=sys.executable,
        args=[str(FIXTURE_SERVER)],
        env=env or {},
    )


async def _sync(
    db, name: str, *, env: dict[str, str] | None = None
) -> tuple[MCPService, UserContext, str]:
    service = MCPService(db)
    actor = _admin()
    server = await service.create_server(_stdio_payload(name, env=env), actor)
    await service.sync(server.id, actor)
    return service, actor, server.id


@pytest.fixture(autouse=True)
async def _ensure_schema(db):
    await init_db()
    yield


async def test_stdio_sync_is_idempotent_and_tools_are_prefixed(db) -> None:
    service, actor, server_id = await _sync(db, _server_name())

    result = await service.sync(server_id, actor)
    assert result.created == 0
    assert result.updated == 0
    assert result.unchanged == 2
    assert result.disabled == 0

    tools = await service.list_server_tools(server_id, actor)
    by_remote = {tool.config["remote_name"]: tool for tool in tools}
    assert set(by_remote) == {"echo", "add"}

    echo = by_remote["echo"]
    assert echo.name.startswith("mcp__")
    assert echo.name.endswith("__echo")
    assert echo.source_type == "mcp"
    assert echo.risk_level == "low"
    assert echo.version == "1"
    assert echo.current_version == 1
    assert echo.permission_scope == []
    assert echo.config["server_id"] == server_id
    assert echo.input_schema["required"] == ["text"]

    add = by_remote["add"]
    assert add.output_schema == {}

    server = await service.get_server(server_id, actor)
    assert server.last_error is None
    assert server.last_sync_at is not None


async def test_execute_stdio_mcp_tools_through_tool_service(db) -> None:
    service, actor, server_id = await _sync(db, _server_name())
    tools = {tool.config["remote_name"]: tool for tool in await service.list_server_tools(
        server_id, actor
    )}
    tool_service = ToolService(db)

    echo = await tool_service.execute(
        ToolInvocation(
            tool_name=tools["echo"].name,
            arguments={"text": "hello"},
            user_id=actor.user_id,
        ),
        actor,
    )
    assert echo.ok is True
    assert echo.status == "success"
    assert echo.output == {"result": "hello"}

    added = await tool_service.execute(
        ToolInvocation(
            tool_name=tools["add"].name,
            arguments={"a": 2, "b": 3},
            user_id=actor.user_id,
        ),
        actor,
    )
    assert added.ok is True
    assert json.loads(added.output) == {"sum": 5}


async def test_disabled_server_denies_execution(db) -> None:
    service, actor, server_id = await _sync(db, _server_name())
    tools = {
        tool.config["remote_name"]: tool
        for tool in await service.list_server_tools(server_id, actor)
    }
    tool = tools["echo"]

    await service.update_server(server_id, MCPServerUpdate(enabled=False), actor)
    result = await ToolService(db).execute(
        ToolInvocation(
            tool_name=tool.name,
            arguments={"text": "nope"},
            user_id=actor.user_id,
        ),
        actor,
    )
    assert result.ok is False
    assert result.status == "failed"
    assert "disabled" in (result.error or "")


async def test_sync_versions_schema_changes_and_disables_missing_tools(db) -> None:
    service, actor, server_id = await _sync(db, _server_name())

    before = {
        tool.config["remote_name"]: tool
        for tool in await service.list_server_tools(server_id, actor)
    }
    assert before["echo"].version == "1"

    await service.update_server(
        server_id, MCPServerUpdate(env={"MCP_ECHO_VARIANT": "v2"}), actor
    )
    changed = await service.sync(server_id, actor)
    assert changed.created == 0
    assert changed.updated >= 1
    after = {
        tool.config["remote_name"]: tool
        for tool in await service.list_server_tools(server_id, actor)
    }
    assert after["echo"].version == "2"
    assert after["echo"].current_version == 2
    assert "uppercase" in after["echo"].input_schema["properties"]

    await service.update_server(
        server_id, MCPServerUpdate(env={"MCP_ECHO_VARIANT": "v3"}), actor
    )
    missing = await service.sync(server_id, actor)
    assert missing.disabled == 1
    after_missing = {
        tool.config["remote_name"]: tool
        for tool in await service.list_server_tools(server_id, actor)
    }
    assert after_missing["add"].enabled is False
    assert after_missing["echo"].enabled is True

    await service.update_server(
        server_id, MCPServerUpdate(env={"MCP_ECHO_VARIANT": "v1"}), actor
    )
    restored = await service.sync(server_id, actor)
    assert restored.disabled == 0
    after_restored = {
        tool.config["remote_name"]: tool
        for tool in await service.list_server_tools(server_id, actor)
    }
    assert after_restored["add"].enabled is True


async def test_sync_failure_records_error_and_leaves_tools_untouched(db) -> None:
    service = MCPService(db)
    actor = _admin()
    server = await service.create_server(
        MCPServerCreate(
            name=_server_name(),
            transport="stdio",
            command="/nonexistent/mcp-binary-for-tests",
        ),
        actor,
    )

    with pytest.raises(ToolError):
        await service.sync(server.id, actor)

    stored = await service.get_server(server.id, actor)
    assert stored.last_error
    assert stored.last_sync_at is None
    tools = await db.scalars(
        select(Tool).where(Tool.source_type == "mcp", Tool.config["server_id"].as_string() == server.id)
    )
    assert list(tools.all()) == []


async def test_non_admin_cannot_manage_servers(db) -> None:
    service = MCPService(db)
    developer = UserContext(
        user_id=str(uuid4()),
        username="dev",
        permissions={"tools:read", "tools:write"},
    )
    with pytest.raises(PermissionDeniedError):
        await service.list_servers(developer)


async def test_server_secret_refs_and_url_validation_reject_unsafe_values() -> None:
    with pytest.raises(PydanticValidationError):
        MCPServerCreate(
            name="bad-header",
            transport="http",
            url="https://mcp.example.test/mcp",
            headers={"Authorization": "${NOT_A_TOOL_SECRET}"},
        )
    with pytest.raises(PydanticValidationError):
        MCPServerCreate(
            name="bad-header-literal",
            transport="http",
            url="https://mcp.example.test/mcp",
            headers={"Authorization": "Bearer literal"},
        )
    with pytest.raises(PydanticValidationError):
        MCPServerCreate(
            name="bad-env-ref",
            transport="stdio",
            command="python",
            env={"MCP_TOKEN": "${WRONG_PREFIX}"},
        )
    with pytest.raises(PydanticValidationError):
        MCPServerCreate(
            name="bad-env-literal",
            transport="stdio",
            command="python",
            env={"API_KEY": "literal-secret"},
        )
    with pytest.raises(PydanticValidationError):
        MCPServerCreate(
            name="bad-env-key",
            transport="stdio",
            command="python",
            env={"NOT-A-VAR": "value"},
        )
    with pytest.raises(PydanticValidationError):
        MCPServerCreate(
            name="bad-url-scheme",
            transport="http",
            url="ftp://mcp.example.test/mcp",
        )
    with pytest.raises(PydanticValidationError):
        MCPServerCreate(
            name="bad-url-creds",
            transport="http",
            url="https://user:pass@mcp.example.test/mcp",
        )
    with pytest.raises(PydanticValidationError):
        MCPServerCreate(name="no-command", transport="stdio")
    with pytest.raises(PydanticValidationError):
        MCPServerCreate(
            name="mixed-transport",
            transport="http",
            url="https://mcp.example.test/mcp",
            command="python",
        )

    valid = MCPServerCreate(
        name="valid-http",
        transport="http",
        url="http://127.0.0.1:9/mcp",
        headers={"Authorization": "${TOOL_SECRET_MCP_TOKEN}"},
    )
    assert valid.headers == {"Authorization": "${TOOL_SECRET_MCP_TOKEN}"}


async def test_secret_mapping_redacts_literals_and_keeps_refs(db) -> None:
    service = MCPService(db)
    actor = _admin()
    server = await service.create_server(
        MCPServerCreate(
            name=_server_name(),
            transport="stdio",
            command="python",
            env={
                "MCP_ECHO_VARIANT": "v1",
                "MCP_TOKEN": "${TOOL_SECRET_MCP_TOKEN}",
            },
        ),
        actor,
    )
    # Admin responses expose stored templates (validated to contain no secrets).
    assert server.env["MCP_ECHO_VARIANT"] == "v1"
    assert server.env["MCP_TOKEN"] == "${TOOL_SECRET_MCP_TOKEN}"

    row = await db.get(MCPServer, server.id)
    assert row is not None
    public = service._out(
        row, UserContext(user_id="viewer", username="viewer")
    )
    assert public.env["MCP_ECHO_VARIANT"] == "***"
    assert public.env["MCP_TOKEN"] == "${TOOL_SECRET_MCP_TOKEN}"


def test_manual_mcp_tool_creation_is_rejected() -> None:
    with pytest.raises(PydanticValidationError):
        ToolCreate(name="manual-mcp-tool", source_type="mcp")


def test_call_result_normalization_prefers_structured_content() -> None:
    result = types.CallToolResult(
        content=[types.TextContent(type="text", text="fallback")],
        structured_content={"ok": True},
        is_error=False,
    )
    assert normalize_call_result(result) == {"ok": True}

    text = "x" * (MAX_RESULT_CHARS + 10)
    result = types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        is_error=False,
    )
    truncated = normalize_call_result(result)
    assert truncated.endswith("...[truncated]")
    assert len(truncated) <= MAX_RESULT_CHARS

    failure = types.CallToolResult(
        content=[types.TextContent(type="text", text="remote boom")],
        is_error=True,
    )
    with pytest.raises(ToolExecutionError, match="remote boom"):
        normalize_call_result(failure)


def test_synced_tool_name_is_deterministic_and_bounded() -> None:
    assert synced_tool_name("echo-tools", "echo") == "mcp__echo-tools__echo"
    assert synced_tool_name("echo-tools", "weird name/with spaces") == (
        "mcp__echo-tools__weird_name_with_spaces"
    )
    long_name = synced_tool_name("s" * 120, "t" * 120)
    assert len(long_name) == 120
    assert long_name == synced_tool_name("s" * 120, "t" * 120)


async def test_mcp_server_delete_removes_discovered_tools(db) -> None:
    service, actor, server_id = await _sync(db, _server_name())
    tools = await service.list_server_tools(server_id, actor)
    assert tools

    await service.delete_server(server_id, actor)
    assert await db.scalar(select(MCPServer).where(MCPServer.id == server_id)) is None
    remaining = await db.scalars(
        select(Tool).where(Tool.source_type == "mcp", Tool.config["server_id"].as_string() == server_id)
    )
    assert list(remaining.all()) == []


async def test_manual_delete_of_mcp_tool_is_forbidden(db) -> None:
    service, actor, server_id = await _sync(db, _server_name())
    tool = (await service.list_server_tools(server_id, actor))[0]
    with pytest.raises(ToolDefinitionError):
        await ToolService(db).delete_tool(tool.id, actor)


async def test_mcp_server_api_crud(client, admin) -> None:
    name = _server_name()
    payload = {
        "name": name,
        "transport": "stdio",
        "command": sys.executable,
        "args": [str(FIXTURE_SERVER)],
        "env": {"MCP_TOKEN": "${TOOL_SECRET_MCP_TOKEN}"},
    }
    created = await client.post("/api/v1/mcp-servers", json=payload, headers=admin)
    assert created.status_code == 201, created.text
    server_id = created.json()["id"]
    assert created.json()["env"]["MCP_TOKEN"] == "${TOOL_SECRET_MCP_TOKEN}"

    listed = await client.get("/api/v1/mcp-servers", headers=admin)
    assert listed.status_code == 200
    assert any(item["id"] == server_id for item in listed.json())

    fetched = await client.get(f"/api/v1/mcp-servers/{server_id}", headers=admin)
    assert fetched.status_code == 200
    assert fetched.json()["name"] == name

    patched = await client.patch(
        f"/api/v1/mcp-servers/{server_id}",
        json={"enabled": False},
        headers=admin,
    )
    assert patched.status_code == 200
    assert patched.json()["enabled"] is False

    tools = await client.get(f"/api/v1/mcp-servers/{server_id}/tools", headers=admin)
    assert tools.status_code == 200
    assert tools.json() == []

    deleted = await client.delete(f"/api/v1/mcp-servers/{server_id}", headers=admin)
    assert deleted.status_code == 204

    unauthenticated = await client.get("/api/v1/mcp-servers")
    assert unauthenticated.status_code == 401
