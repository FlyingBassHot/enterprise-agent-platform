from __future__ import annotations

import asyncio
import ipaddress
import uuid
from datetime import datetime
from typing import Any

import httpx
import pytest
from httpx import AsyncClient

from app.core.config import get_settings
from app.core.contracts import ToolContext, ToolDescriptor, UserContext
from app.core.exceptions import PermissionDeniedError, ToolError
from app.modules.execution.service import _TASKS
from app.modules.runtime.checkpoint import close_checkpointer
from app.modules.tool.builtin import TOOL_NAMES, register_builtin_tools
from app.modules.tool.builtin import http as builtin_http
from app.modules.tool.builtin.artifacts import read_artifact_tool
from app.modules.tool.builtin.time import current_time_tool
from app.modules.tool.models import Tool
from app.modules.tool.registry import NativeToolRegistry
from app.modules.tool.service import ToolService

API = "/api/v1"


def _ctx(user: UserContext | None = None, tool_name: str = "platform_current_time") -> ToolContext:
    return ToolContext(
        user=user or UserContext(user_id="u-1", username="tester"),
        tool_name=tool_name,
    )


@pytest.fixture(autouse=True)
async def _drain_execution_tasks():
    yield
    tasks = [task for task in _TASKS.values() if not task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    await close_checkpointer()


async def test_builtin_descriptors_register() -> None:
    registry = NativeToolRegistry()
    descriptors = register_builtin_tools(registry)
    assert sorted(registry.names()) == sorted(TOOL_NAMES)
    assert [descriptor.name for descriptor in descriptors] == list(TOOL_NAMES)
    by_name = {descriptor.name: descriptor for descriptor in descriptors}
    assert by_name["platform_current_time"].risk_level == "low"
    assert by_name["platform_read_artifact"].risk_level == "low"
    assert by_name["platform_http_request"].risk_level == "medium"
    assert by_name["platform_http_request"].requires_approval is False
    http_schema = by_name["platform_http_request"].input_schema
    assert http_schema["properties"]["method"]["enum"] == ["GET", "POST"]
    assert http_schema["required"] == ["url"]


async def test_current_time_defaults_to_utc() -> None:
    output = await current_time_tool({}, _ctx())
    assert output["timezone"] == "UTC"
    parsed = datetime.fromisoformat(output["iso8601"])
    assert parsed.utcoffset().total_seconds() == 0
    assert output["utc_offset"] == "+00:00"
    assert isinstance(output["epoch_seconds"], int)


async def test_current_time_uses_iana_zone() -> None:
    output = await current_time_tool({"tz": "Asia/Shanghai"}, _ctx())
    assert output["utc_offset"] == "+08:00"
    assert datetime.fromisoformat(output["iso8601"]).utcoffset().total_seconds() == 8 * 3600


async def test_current_time_rejects_unknown_zone() -> None:
    with pytest.raises(ToolError):
        await current_time_tool({"tz": "Mars/Olympus"}, _ctx())


async def test_read_artifact_returns_truncated_text(
    client: AsyncClient, admin: dict[str, str], db
) -> None:
    from app.modules.permission.service import PermissionService

    body = ("col_a,col_b\n" + "\n".join(f"{index},{index * 2}" for index in range(120))).encode()
    upload = await client.post(
        f"{API}/artifacts",
        headers=admin,
        files={"file": ("rows.csv", body, "text/csv")},
    )
    assert upload.status_code == 201, upload.text
    artifact = upload.json()

    permissions = PermissionService(db)
    user = await permissions.repo.get_user_by_username("admin")
    assert user is not None
    actor = await permissions.load_context(user.id)

    output = await read_artifact_tool(
        {"artifact_id": artifact["id"], "max_chars": 500},
        _ctx(actor, tool_name="platform_read_artifact"),
    )
    assert output["filename"] == "rows.csv"
    assert output["size"] == len(body)
    assert output["truncated"] is True
    assert len(output["content"]) == 500
    assert output["total_chars"] > 500
    assert output["content"].startswith("col_a,col_b")


async def test_read_artifact_denies_other_user(
    client: AsyncClient, admin: dict[str, str], db
) -> None:
    from app.modules.permission.service import PermissionService

    upload = await client.post(
        f"{API}/artifacts",
        headers=admin,
        files={"file": ("secret.md", "# private notes", "text/markdown")},
    )
    assert upload.status_code == 201, upload.text

    username = f"viewer_{uuid.uuid4().hex[:8]}"
    created = await client.post(
        f"{API}/users",
        headers=admin,
        json={"username": username, "password": "viewer-pass-123", "role_names": ["viewer"]},
    )
    assert created.status_code == 201, created.text

    permissions = PermissionService(db)
    other = await permissions.repo.get_user_by_username(username)
    assert other is not None
    actor = await permissions.load_context(other.id)

    with pytest.raises(PermissionDeniedError):
        await read_artifact_tool(
            {"artifact_id": upload.json()["id"]},
            _ctx(actor, tool_name="platform_read_artifact"),
        )


async def test_http_request_denied_without_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "http_tool_allowed_hosts", [])
    with pytest.raises(ToolError, match="disabled"):
        await builtin_http.http_request_tool(
            {"url": "https://api.example.com/health"},
            _ctx(tool_name="platform_http_request"),
        )


async def test_http_request_blocks_private_address(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "http_tool_allowed_hosts", ["localhost"])

    async def _resolve(host: str, port: int) -> list[ipaddress.IPv4Address]:
        return [ipaddress.ip_address("127.0.0.1")]

    monkeypatch.setattr(builtin_http, "_resolve_addresses", _resolve)
    with pytest.raises(ToolError, match="non-public"):
        await builtin_http.http_request_tool(
            {"url": "http://localhost:8080/admin"},
            _ctx(tool_name="platform_http_request"),
        )


async def test_http_request_rejects_unlisted_host_in_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "http_tool_allowed_hosts", [".example.com"])

    async def _resolve(host: str, port: int) -> list[ipaddress.IPv4Address]:
        return [ipaddress.ip_address("93.184.216.34")]

    monkeypatch.setattr(builtin_http, "_resolve_addresses", _resolve)
    with pytest.raises(ToolError, match="not in APP_HTTP_TOOL_ALLOWED_HOSTS"):
        await builtin_http.http_request_tool(
            {"url": "https://api.example.org/data"},
            _ctx(tool_name="platform_http_request"),
        )


async def test_http_request_caps_response_and_does_not_follow_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "http_tool_allowed_hosts", ["api.example.com"])
    monkeypatch.setattr(settings, "http_tool_max_response_kb", 1)
    calls: list[httpx.Request] = []

    async def _resolve(host: str, port: int) -> list[ipaddress.IPv4Address]:
        return [ipaddress.ip_address("93.184.216.34")]

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/plain; charset=utf-8"},
            content=b"x" * 5000,
        )

    def _client(timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(_handler),
            timeout=httpx.Timeout(timeout),
            follow_redirects=False,
        )

    monkeypatch.setattr(builtin_http, "_resolve_addresses", _resolve)
    monkeypatch.setattr(builtin_http, "build_client", _client)
    output = await builtin_http.http_request_tool(
        {"url": "https://api.example.com/resource", "headers": {"X-Trace": "abc"}},
        _ctx(tool_name="platform_http_request"),
    )
    assert output["status"] == 200
    assert output["truncated"] is True
    assert len(output["text"]) == 1024
    assert calls[0].headers["X-Trace"] == "abc"

    def _redirect(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(302, headers={"location": "https://evil.example.net/"})

    monkeypatch.setattr(
        builtin_http,
        "build_client",
        lambda timeout: httpx.AsyncClient(
            transport=httpx.MockTransport(_redirect),
            timeout=httpx.Timeout(timeout),
            follow_redirects=False,
        ),
    )
    redirected = await builtin_http.http_request_tool(
        {"url": "https://api.example.com/go"},
        _ctx(tool_name="platform_http_request"),
    )
    assert redirected["status"] == 302
    assert redirected["location"] == "https://evil.example.net/"
    assert len(calls) == 2


async def test_http_request_forbids_host_header(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "http_tool_allowed_hosts", ["api.example.com"])
    with pytest.raises(ToolError, match="must not be set"):
        await builtin_http.http_request_tool(
            {"url": "https://api.example.com/", "headers": {"Host": "internal"}},
            _ctx(tool_name="platform_http_request"),
        )


async def test_sync_native_skips_conflicting_source_type(
    client: AsyncClient, db, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = f"platform_probe_{uuid.uuid4().hex[:8]}"

    async def _handler(arguments: dict[str, Any], ctx: ToolContext) -> Any:
        return {}

    registry = NativeToolRegistry()
    registry.register(
        ToolDescriptor(
            name=name,
            description="probe",
            input_schema={"type": "object", "properties": {}},
            risk_level="low",
            timeout_seconds=5,
        ),
        _handler,
    )
    db.add(
        Tool(
            name=name,
            display_name=name,
            description="operator-owned http tool",
            version="1.0.0",
            source_type="http",
            input_schema={"type": "object", "properties": {}},
            risk_level="low",
            timeout_seconds=10,
            config={"method": "GET", "url": "https://example.com", "headers": {}, "query": {}},
            enabled=True,
        )
    )
    await db.flush()

    monkeypatch.setattr("app.modules.tool.service.native_registry", registry)
    synced = await ToolService(db).sync_native()
    assert synced == 0

    stored = await ToolService(db).repo.get_by_name(name)
    assert stored is not None
    assert stored.source_type == "http"


async def test_seed_general_assistant_binds_baseline_tools(
    client: AsyncClient, admin: dict[str, str], db
) -> None:
    from app.modules.agent.seed import seed_general_assistant
    from app.modules.agent.service import AgentService
    from app.modules.model.schemas import ModelConfigCreate, ProviderCreate
    from app.modules.model.service import ModelService
    from app.modules.permission.service import PermissionService
    from app.modules.prompt.service import PromptService

    suffix = uuid.uuid4().hex[:8]
    models = ModelService(db)
    provider = await models.create_provider(
        ProviderCreate(name=f"seed-provider-{suffix}", provider_type="mock")
    )
    config = await models.create_config(
        ModelConfigCreate(
            name=f"seed-config-{suffix}",
            provider_id=provider.id,
            model_name="mock-agent",
        )
    )
    permissions = PermissionService(db)
    user = await permissions.repo.get_user_by_username("admin")
    assert user is not None
    actor = await permissions.load_context(user.id)

    result = await seed_general_assistant(
        AgentService(db), PromptService(db), ToolService(db), actor, config.id
    )
    assert result["agent_status"] == "published"
    assert result["tool_names"] == list(TOOL_NAMES)

    again = await seed_general_assistant(
        AgentService(db), PromptService(db), ToolService(db), actor, config.id
    )
    assert again["agent_version_id"] == result["agent_version_id"]

    agent = await AgentService(db).get_agent(result["agent_id"], actor)
    assert agent.draft_config.tools == list(TOOL_NAMES)
    assert agent.status == "published"


async def test_builtin_time_tool_runs_end_to_end(
    client: AsyncClient, admin: dict[str, str]
) -> None:
    suffix = uuid.uuid4().hex[:8]
    provider = await client.post(
        f"{API}/model-providers",
        headers=admin,
        json={"name": f"mock-provider-{suffix}", "provider_type": "mock"},
    )
    assert provider.status_code == 201, provider.text
    script = [
        {
            "tool_calls": [
                {"name": "platform_current_time", "arguments": {"tz": "Asia/Shanghai"}}
            ]
        },
        {"content": "当前时间已获取"},
    ]
    config = await client.post(
        f"{API}/model-configs",
        headers=admin,
        json={
            "name": f"mock-config-{suffix}",
            "provider_id": provider.json()["id"],
            "model_name": "mock-agent",
            "extra": {"script": script},
        },
    )
    assert config.status_code == 201, config.text
    prompt = await client.post(
        f"{API}/prompts", headers=admin, json={"name": f"prompt-{suffix}"}
    )
    assert prompt.status_code == 201, prompt.text
    prompt_id = prompt.json()["id"]
    version = await client.post(
        f"{API}/prompts/{prompt_id}/versions",
        headers=admin,
        json={"system_prompt": "你是端到端测试助手", "publish": True},
    )
    assert version.status_code == 201, version.text
    agent = await client.post(
        f"{API}/agents",
        headers=admin,
        json={
            "name": f"agent-{suffix}",
            "config": {
                "model": {"config_id": config.json()["id"]},
                "prompt": {"prompt_id": prompt_id, "version_id": version.json()["id"]},
                "tools": list(TOOL_NAMES),
                "retrieval": {"enabled": False},
            },
        },
    )
    assert agent.status_code == 201, agent.text
    agent_id = agent.json()["id"]
    published = await client.post(f"{API}/agents/{agent_id}/publish", headers=admin)
    assert published.status_code == 200, published.text

    execution = await client.post(
        f"{API}/executions", headers=admin, json={"agent_id": agent_id, "input": "现在几点？"}
    )
    assert execution.status_code == 201, execution.text
    execution_id = execution.json()["id"]

    loop = asyncio.get_running_loop()
    deadline = loop.time() + 15
    detail: dict[str, Any] = {}
    while loop.time() < deadline:
        response = await client.get(f"{API}/executions/{execution_id}", headers=admin)
        assert response.status_code == 200, response.text
        detail = response.json()
        if detail["status"] in {"completed", "failed", "cancelled"}:
            break
        await asyncio.sleep(0.02)
    assert detail.get("status") == "completed", detail
    assert detail["output_text"] == "当前时间已获取"

    events = await client.get(f"{API}/executions/{execution_id}/events", headers=admin)
    assert events.status_code == 200, events.text
    completed = [
        event
        for event in events.json()
        if event["type"] == "tool_call_completed"
        and event["payload"].get("tool_name") == "platform_current_time"
    ]
    assert completed, events.json()
    assert completed[0]["payload"]["status"] in {"success", "completed"}
