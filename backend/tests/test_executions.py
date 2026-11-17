from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi import Depends
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.session import get_db
from app.main import app
from app.modules.execution.router import get_execution_service
from app.modules.execution.service import _TASKS, ExecutionService
from app.modules.runtime.checkpoint import close_checkpointer
from tests.test_runtime import (
    FakeLoader,
    FakeModel,
    FakeTools,
    make_snapshot,
    make_tool,
)

API = "/api/v1"


@pytest.fixture(autouse=True)
async def _cleanup_runtime_state():
    yield
    tasks = [task for task in _TASKS.values() if not task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    app.dependency_overrides.pop(get_execution_service, None)
    await close_checkpointer()


@pytest.fixture
def install_runtime():
    def _install(*, model=None, tools=None, loader=None):
        loader = loader or FakeLoader(make_snapshot())
        tools = tools or FakeTools()
        model = model or FakeModel([{"content": "hello from fake model"}])

        async def _dependency(db: AsyncSession = Depends(get_db)) -> ExecutionService:
            return ExecutionService(
                db, agent_loader=loader, tool_gateway=tools, model_gateway=model
            )

        app.dependency_overrides[get_execution_service] = _dependency
        return model, tools, loader

    return _install


async def _wait_for_status(
    client: AsyncClient,
    headers: dict[str, str],
    execution_id: str,
    statuses: set[str],
    *,
    timeout: float = 10.0,
) -> dict:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    seen = None
    while loop.time() < deadline:
        response = await client.get(f"{API}/executions/{execution_id}", headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        seen = body
        if body["status"] in statuses:
            return body
        await asyncio.sleep(0.02)
    raise AssertionError(f"execution never reached {statuses}; last={seen}")


async def _create_execution(
    client: AsyncClient, headers: dict[str, str], **overrides
) -> dict:
    payload = {"agent_id": "agent-1", "input": "hello", **overrides}
    response = await client.post(f"{API}/executions", headers=headers, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


async def _create_viewer(client: AsyncClient, admin: dict[str, str]) -> dict[str, str]:
    username = f"viewer_{uuid.uuid4().hex[:8]}"
    response = await client.post(
        f"{API}/users",
        headers=admin,
        json={
            "username": username,
            "password": "viewer-pass-123",
            "role_names": ["viewer"],
        },
    )
    assert response.status_code == 201, response.text
    login = await client.post(
        f"{API}/auth/login", json={"username": username, "password": "viewer-pass-123"}
    )
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


async def test_execution_completes_and_streams_events(
    client: AsyncClient, admin: dict[str, str], install_runtime
) -> None:
    install_runtime()
    execution = await _create_execution(client, admin, input="say hello")
    assert execution["status"] == "pending"
    assert execution["conversation_id"]

    finished = await _wait_for_status(client, admin, execution["id"], {"completed"})
    assert finished["output_text"] == "hello from fake model"
    assert finished["token_usage"]["total_tokens"] == 10
    assert finished["error"] is None
    assert finished["llm_calls"] and finished["llm_calls"][0]["provider"] == "fake-provider"
    assert finished["events_count"] >= 4
    assert finished["approvals"] == []

    events_response = await client.get(
        f"{API}/executions/{execution['id']}/events", headers=admin
    )
    assert events_response.status_code == 200, events_response.text
    events = events_response.json()
    types = [event["type"] for event in events]
    assert types[0] == "execution_started"
    assert types[-1] == "execution_completed"
    assert "message_delta" in types
    assert "message_completed" in types
    assert [event["seq"] for event in events] == sorted(event["seq"] for event in events)

    after_response = await client.get(
        f"{API}/executions/{execution['id']}/events?after_seq=1", headers=admin
    )
    assert all(event["seq"] > 1 for event in after_response.json())

    messages_response = await client.get(
        f"{API}/conversations/{execution['conversation_id']}/messages", headers=admin
    )
    assert messages_response.status_code == 200, messages_response.text
    messages = messages_response.json()
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[1]["content"] == "hello from fake model"
    assert messages[0]["execution_id"] == execution["id"]

    async with client.stream(
        "GET", f"{API}/executions/{execution['id']}/stream", headers=admin
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = ""
        async for chunk in response.aiter_text():
            body += chunk
    assert f"id: {events[0]['seq']}" in body
    assert "event: execution_started" in body
    assert "event: execution_completed" in body

    resume_after = events[1]["seq"]
    async with client.stream(
        "GET",
        f"{API}/executions/{execution['id']}/stream",
        headers={**admin, "Last-Event-ID": str(resume_after)},
    ) as response:
        resumed_body = ""
        async for chunk in response.aiter_text():
            resumed_body += chunk
    assert f"id: {events[0]['seq']}\n" not in resumed_body
    assert f"id: {resume_after}\n" not in resumed_body
    assert f"id: {events[2]['seq']}" in resumed_body
    assert "event: execution_completed" in resumed_body


async def test_tool_call_events_and_final_answer(
    client: AsyncClient, admin: dict[str, str], install_runtime
) -> None:
    tools = FakeTools([make_tool("echo", "low")])
    model = FakeModel(
        [
            {"tool_calls": [{"name": "echo", "arguments": {"value": 7}}]},
            {"content": "tool finished"},
        ]
    )
    install_runtime(
        model=model,
        tools=tools,
        loader=FakeLoader(make_snapshot(tool_names=["echo"])),
    )

    execution = await _create_execution(client, admin, input="use echo")
    finished = await _wait_for_status(client, admin, execution["id"], {"completed"})
    assert finished["output_text"] == "tool finished"
    assert len(finished["llm_calls"]) == 2
    assert len(tools.invocations) == 1
    assert tools.invocations[0].arguments == {"value": 7}

    events = (
        await client.get(f"{API}/executions/{execution['id']}/events", headers=admin)
    ).json()
    types = [event["type"] for event in events]
    assert "tool_call_started" in types
    assert "tool_call_completed" in types
    completed = next(event for event in events if event["type"] == "tool_call_completed")
    assert completed["payload"]["ok"] is True


async def test_cancel_running_execution(
    client: AsyncClient, admin: dict[str, str], install_runtime
) -> None:
    install_runtime(model=FakeModel([{"content": "never delivered"}], delay=2.0))
    execution = await _create_execution(client, admin, input="slow")
    await _wait_for_status(client, admin, execution["id"], {"running"})

    response = await client.post(
        f"{API}/executions/{execution['id']}/cancel", headers=admin
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "cancelled"

    await asyncio.sleep(0.2)
    detail = (
        await client.get(f"{API}/executions/{execution['id']}", headers=admin)
    ).json()
    assert detail["status"] == "cancelled"
    events = (
        await client.get(f"{API}/executions/{execution['id']}/events", headers=admin)
    ).json()
    types = [event["type"] for event in events]
    assert "execution_completed" not in types
    assert types[-1] == "execution_cancelled"


async def test_conversation_endpoints(
    client: AsyncClient, admin: dict[str, str], install_runtime
) -> None:
    install_runtime()
    created = await client.post(
        f"{API}/conversations",
        headers=admin,
        json={"agent_id": "agent-1", "title": "Audit chat"},
    )
    assert created.status_code == 201, created.text
    conversation = created.json()
    assert conversation["title"] == "Audit chat"

    listed = await client.get(f"{API}/conversations", headers=admin)
    assert listed.status_code == 200
    assert any(item["id"] == conversation["id"] for item in listed.json())

    fetched = await client.get(
        f"{API}/conversations/{conversation['id']}", headers=admin
    )
    assert fetched.status_code == 200
    messages = await client.get(
        f"{API}/conversations/{conversation['id']}/messages", headers=admin
    )
    assert messages.status_code == 200
    assert messages.json() == []


async def test_artifact_upload_meta_and_download(
    client: AsyncClient, admin: dict[str, str], install_runtime
) -> None:
    install_runtime()
    upload = await client.post(
        f"{API}/artifacts",
        headers=admin,
        files={"file": ("transactions.txt", b"amount,rule\n10,test\n", "text/plain")},
    )
    assert upload.status_code == 201, upload.text
    artifact = upload.json()
    assert artifact["filename"] == "transactions.txt"
    assert artifact["size"] == len(b"amount,rule\n10,test\n")

    meta = await client.get(f"{API}/artifacts/{artifact['id']}/meta", headers=admin)
    assert meta.status_code == 200
    assert meta.json()["storage_key"].startswith("artifacts/")

    download = await client.get(f"{API}/artifacts/{artifact['id']}", headers=admin)
    assert download.status_code == 200
    assert download.content == b"amount,rule\n10,test\n"

    rejected = await client.post(
        f"{API}/artifacts",
        headers=admin,
        files={"file": ("payload.exe", b"nope", "application/octet-stream")},
    )
    assert rejected.status_code == 422, rejected.text

    viewer = await _create_viewer(client, admin)
    forbidden = await client.get(
        f"{API}/artifacts/{artifact['id']}/meta", headers=viewer
    )
    assert forbidden.status_code == 403, forbidden.text


async def test_non_owner_cannot_read_execution(
    client: AsyncClient, admin: dict[str, str], install_runtime
) -> None:
    install_runtime()
    execution = await _create_execution(client, admin, input="owned by admin")
    await _wait_for_status(client, admin, execution["id"], {"completed"})

    viewer = await _create_viewer(client, admin)
    denied = await client.get(f"{API}/executions/{execution['id']}", headers=viewer)
    assert denied.status_code == 403, denied.text

    listed = await client.get(f"{API}/executions", headers=viewer)
    assert listed.status_code == 200
    assert all(item["id"] != execution["id"] for item in listed.json())

    events = await client.get(
        f"{API}/executions/{execution['id']}/events", headers=viewer
    )
    assert events.status_code == 403


async def test_permission_checks_are_enforced(
    client: AsyncClient, admin: dict[str, str], install_runtime
) -> None:
    install_runtime()
    viewer = await _create_viewer(client, admin)
    response = await client.post(
        f"{API}/executions",
        headers=viewer,
        json={"agent_id": "agent-1", "input": "nope"},
    )
    assert response.status_code == 403, response.text


async def test_conversation_messages_return_newest_window(
    client: AsyncClient, admin: dict[str, str], db: AsyncSession
) -> None:
    from app.modules.execution.repository import ExecutionRepository

    created = await client.post(
        "/api/v1/conversations",
        headers=admin,
        json={"agent_id": "history-agent", "title": "history"},
    )
    assert created.status_code in (200, 201), created.text
    conversation_id = created.json()["id"]
    repo = ExecutionRepository(db)
    for index in range(600):
        await repo.add_message(
            conversation_id=conversation_id,
            role="user",
            content=f"message-{index:03d}",
            execution_id=None,
        )
    await db.commit()

    response = await client.get(
        f"/api/v1/conversations/{conversation_id}/messages", headers=admin
    )
    assert response.status_code == 200, response.text
    contents = [row["content"] for row in response.json()]
    assert len(contents) == 500
    assert contents[-1] == "message-599"
    assert contents[0] == "message-100"
    assert "message-000" not in contents


async def test_budget_exhaustion_is_resumable(
    client: AsyncClient, admin: dict[str, str], install_runtime
) -> None:
    snapshot = make_snapshot(max_iterations=1, tool_names=["echo"])
    model = FakeModel(
        [
            {
                "content": "partial answer",
                "tool_calls": [{"name": "echo", "arguments": {"value": 1}}],
            },
            {"content": "final answer"},
        ]
    )
    tools = FakeTools([make_tool("echo", "low")])
    install_runtime(model=model, tools=tools, loader=FakeLoader(snapshot))

    execution = await _create_execution(client, admin, input="do the job")
    body = await _wait_for_status(client, admin, execution["id"], {"incomplete"})
    assert body["output_text"] == "partial answer"
    assert body["output_payload"]["partial"] is True
    assert body["error"] is None

    events = await client.get(f"{API}/executions/{execution['id']}/events", headers=admin)
    assert events.status_code == 200, events.text
    assert any(item["type"] == "execution_incomplete" for item in events.json())

    resumed = await client.post(
        f"{API}/executions/{execution['id']}/resume",
        headers=admin,
        json={"extra_iterations": 2},
    )
    assert resumed.status_code == 200, resumed.text

    finished = await _wait_for_status(client, admin, execution["id"], {"completed"})
    assert finished["output_text"] == "final answer"
    assert finished["output_payload"].get("partial") is not True


async def test_token_budget_is_resumable(
    client: AsyncClient, admin: dict[str, str], install_runtime
) -> None:
    snapshot = make_snapshot(token_budget=10)
    model = FakeModel([{"content": "partial answer"}])
    install_runtime(model=model, tools=FakeTools(), loader=FakeLoader(snapshot))

    execution = await _create_execution(client, admin, input="do the job")
    body = await _wait_for_status(client, admin, execution["id"], {"incomplete"})
    assert body["output_text"] == "partial answer"
    assert body["output_payload"]["stop_reason"] == "token_budget"
    assert body["output_payload"]["token_budget"] == 10

    resumed = await client.post(
        f"{API}/executions/{execution['id']}/resume",
        headers=admin,
        json={"extra_tokens": 100},
    )
    assert resumed.status_code == 200, resumed.text

    finished = await _wait_for_status(client, admin, execution["id"], {"completed"})
    assert finished["output_text"] == "partial answer"
