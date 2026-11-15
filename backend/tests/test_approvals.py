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
    def _install(*, model, tools, snapshot):
        async def _dependency(db: AsyncSession = Depends(get_db)) -> ExecutionService:
            return ExecutionService(
                db,
                agent_loader=FakeLoader(snapshot),
                tool_gateway=tools,
                model_gateway=model,
            )

        app.dependency_overrides[get_execution_service] = _dependency

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
        seen = response.json()
        if seen["status"] in statuses:
            return seen
        await asyncio.sleep(0.02)
    raise AssertionError(f"execution never reached {statuses}; last={seen}")


async def _start_waiting_execution(
    client: AsyncClient, headers: dict[str, str], **execution_payload
) -> tuple[dict, dict]:
    response = await client.post(
        f"{API}/executions",
        headers=headers,
        json={"agent_id": "agent-approval", "input": "please wire money", **execution_payload},
    )
    assert response.status_code == 201, response.text
    execution = response.json()
    detail = await _wait_for_status(
        client, headers, execution["id"], {"waiting_approval"}
    )
    assert len(detail["approvals"]) == 1
    return execution, detail["approvals"][0]


def _high_risk_setup():
    model = FakeModel(
        [
            {"tool_calls": [{"name": "wire_money", "arguments": {"amount": 100}}]},
            {"content": "all set"},
        ]
    )
    tools = FakeTools([make_tool("wire_money", "high", requires_approval=True)])
    snapshot = make_snapshot(
        agent_id="agent-approval",
        tool_names=["wire_money"],
        system_prompt="Approval test agent",
    )
    return model, tools, snapshot


async def _events(client: AsyncClient, headers: dict[str, str], execution_id: str) -> list[dict]:
    response = await client.get(f"{API}/executions/{execution_id}/events", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def test_approve_resumes_execution(
    client: AsyncClient, admin: dict[str, str], install_runtime
) -> None:
    model, tools, snapshot = _high_risk_setup()
    install_runtime(model=model, tools=tools, snapshot=snapshot)
    execution, approval = await _start_waiting_execution(client, admin)

    assert approval["status"] == "pending"
    assert approval["tool_name"] == "wire_money"

    resume_blocked = await client.post(
        f"{API}/executions/{execution['id']}/resume", headers=admin
    )
    assert resume_blocked.status_code == 409, resume_blocked.text

    approved = await client.post(
        f"{API}/approvals/{approval['id']}/approve",
        headers=admin,
        json={"comment": "verified"},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"
    assert approved.json()["resolved_by"]
    assert approved.json()["comment"] == "verified"

    again = await client.post(
        f"{API}/approvals/{approval['id']}/approve", headers=admin, json={"comment": "twice"}
    )
    assert again.status_code == 409, again.text

    finished = await _wait_for_status(client, admin, execution["id"], {"completed"})
    assert finished["output_text"] == "all set"
    assert len(tools.invocations) == 1
    assert tools.invocations[0].approved is True

    events = await _events(client, admin, execution["id"])
    types = [event["type"] for event in events]
    assert types.count("approval_required") == 1
    assert types.count("approval_resolved") == 1
    assert "tool_call_started" in types
    assert "tool_call_completed" in types
    assert types[-1] == "execution_completed"
    resolved = next(event for event in events if event["type"] == "approval_resolved")
    assert resolved["payload"]["approved"] is True

    pending = await client.get(
        f"{API}/approvals?status=pending&execution_id={execution['id']}", headers=admin
    )
    assert pending.status_code == 200
    assert pending.json() == []


async def test_reject_returns_rejection_to_model(
    client: AsyncClient, admin: dict[str, str], install_runtime
) -> None:
    model = FakeModel(
        [
            {"tool_calls": [{"name": "wire_money", "arguments": {"amount": 900}}]},
            {"content": "understood, transfer cancelled"},
        ]
    )
    tools = FakeTools([make_tool("wire_money", "high", requires_approval=True)])
    snapshot = make_snapshot(
        agent_id="agent-approval",
        tool_names=["wire_money"],
        system_prompt="Approval test agent",
    )
    install_runtime(model=model, tools=tools, snapshot=snapshot)
    execution, approval = await _start_waiting_execution(client, admin)

    rejected = await client.post(
        f"{API}/approvals/{approval['id']}/reject",
        headers=admin,
        json={"comment": "policy violation"},
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "rejected"

    finished = await _wait_for_status(client, admin, execution["id"], {"completed"})
    assert finished["output_text"] == "understood, transfer cancelled"
    assert tools.invocations == []

    events = await _events(client, admin, execution["id"])
    types = [event["type"] for event in events]
    assert "tool_call_started" not in types
    resolved = next(event for event in events if event["type"] == "approval_resolved")
    assert resolved["payload"]["approved"] is False
    assert "policy violation" in resolved["payload"]["comment"]

    tool_messages = [
        message for message in model.calls[-1]["messages"] if message.role == "tool"
    ]
    assert tool_messages
    assert "policy violation" in tool_messages[-1].content


async def test_non_owner_cannot_list_or_resolve_approval(
    client: AsyncClient, admin: dict[str, str], install_runtime
) -> None:
    model, tools, snapshot = _high_risk_setup()
    install_runtime(model=model, tools=tools, snapshot=snapshot)
    execution, approval = await _start_waiting_execution(client, admin)

    role_name = f"approver_{uuid.uuid4().hex[:8]}"
    role = await client.post(
        f"{API}/roles",
        headers=admin,
        json={
            "name": role_name,
            "permission_codes": [
                "executions:read",
                "approvals:read",
                "approvals:write",
            ],
        },
    )
    assert role.status_code == 201, role.text
    username = f"peer_{uuid.uuid4().hex[:8]}"
    user = await client.post(
        f"{API}/users",
        headers=admin,
        json={
            "username": username,
            "password": "peer-pass-123",
            "role_names": [role_name],
        },
    )
    assert user.status_code == 201, user.text
    login = await client.post(
        f"{API}/auth/login", json={"username": username, "password": "peer-pass-123"}
    )
    assert login.status_code == 200, login.text
    peer = {"Authorization": f"Bearer {login.json()['access_token']}"}

    listed = await client.get(f"{API}/approvals?status=pending", headers=peer)
    assert listed.status_code == 200
    assert listed.json() == []

    denied = await client.post(
        f"{API}/approvals/{approval['id']}/approve", headers=peer, json={"comment": "mine"}
    )
    assert denied.status_code == 403, denied.text

    detail = await client.get(f"{API}/executions/{execution['id']}", headers=peer)
    assert detail.status_code == 403, detail.text


async def test_multiple_tool_calls_are_approved_sequentially(
    client: AsyncClient, admin: dict[str, str], install_runtime
) -> None:
    model = FakeModel(
        [
            {
                "tool_calls": [
                    {"name": "wire_money", "arguments": {"amount": 1}},
                    {"name": "wire_money", "arguments": {"amount": 2}},
                ]
            },
            {"content": "both transfers done"},
        ]
    )
    tools = FakeTools([make_tool("wire_money", "high", requires_approval=True)])
    snapshot = make_snapshot(
        agent_id="agent-approval", tool_names=["wire_money"], system_prompt="Batch approvals"
    )
    install_runtime(model=model, tools=tools, snapshot=snapshot)

    response = await client.post(
        f"{API}/executions",
        headers=admin,
        json={"agent_id": "agent-approval", "input": "wire twice"},
    )
    assert response.status_code == 201, response.text
    execution_id = response.json()["id"]

    first = await _wait_for_status(client, admin, execution_id, {"waiting_approval"})
    assert len(first["approvals"]) == 1
    first_approval = first["approvals"][0]

    approve_first = await client.post(
        f"{API}/approvals/{first_approval['id']}/approve", headers=admin
    )
    assert approve_first.status_code == 200, approve_first.text

    loop = asyncio.get_running_loop()
    deadline = loop.time() + 10
    second_approval = None
    while loop.time() < deadline:
        detail = (
            await client.get(f"{API}/executions/{execution_id}", headers=admin)
        ).json()
        pending = [row for row in detail["approvals"] if row["status"] == "pending"]
        if detail["status"] == "waiting_approval" and pending:
            second_approval = pending[0]
            break
        await asyncio.sleep(0.02)
    assert second_approval is not None
    assert second_approval["id"] != first_approval["id"]

    approve_second = await client.post(
        f"{API}/approvals/{second_approval['id']}/approve", headers=admin
    )
    assert approve_second.status_code == 200, approve_second.text

    finished = await _wait_for_status(client, admin, execution_id, {"completed"})
    assert finished["output_text"] == "both transfers done"
    assert [invocation.arguments for invocation in tools.invocations] == [
        {"amount": 1},
        {"amount": 2},
    ]
    events = await _events(client, admin, execution_id)
    types = [event["type"] for event in events]
    assert types.count("approval_required") == 2
    assert types.count("approval_resolved") == 2


async def test_execution_owner_cannot_self_approve_non_superuser(
    client: AsyncClient, admin: dict[str, str], install_runtime
) -> None:
    await client.post(
        f"{API}/users",
        headers=admin,
        json={
            "username": "approval-developer",
            "password": "developer-pass-123",
            "role_names": ["developer"],
        },
    )
    login = await client.post(
        f"{API}/auth/login",
        json={"username": "approval-developer", "password": "developer-pass-123"},
    )
    developer = {"Authorization": f"Bearer {login.json()['access_token']}"}

    model, tools, snapshot = _high_risk_setup()
    install_runtime(model=model, tools=tools, snapshot=snapshot)
    execution, approval = await _start_waiting_execution(client, developer)

    self_approval = await client.post(
        f"{API}/approvals/{approval['id']}/approve", headers=developer, json={}
    )
    assert self_approval.status_code == 403, self_approval.text

    await client.post(
        f"{API}/users",
        headers=admin,
        json={
            "username": "approval-approver",
            "password": "approver-pass-123",
            "role_names": ["approver"],
        },
    )
    approver_login = await client.post(
        f"{API}/auth/login",
        json={"username": "approval-approver", "password": "approver-pass-123"},
    )
    approver = {
        "Authorization": f"Bearer {approver_login.json()['access_token']}"
    }
    context = await client.get(
        f"{API}/executions/{execution['id']}", headers=approver
    )
    assert context.status_code == 200, context.text
    events = await client.get(
        f"{API}/executions/{execution['id']}/events", headers=approver
    )
    assert events.status_code == 200, events.text

    visible = await client.get(
        f"{API}/approvals?status=pending&execution_id={execution['id']}",
        headers=approver,
    )
    assert visible.status_code == 200
    assert visible.json()[0]["id"] == approval["id"]

    approved = await client.post(
        f"{API}/approvals/{approval['id']}/approve", headers=approver, json={}
    )
    assert approved.status_code == 200, approved.text
    duplicate = await client.post(
        f"{API}/approvals/{approval['id']}/approve", headers=approver, json={}
    )
    assert duplicate.status_code == 409, duplicate.text
    await _wait_for_status(client, developer, execution["id"], {"completed"})


async def test_operator_can_run_agent_without_agents_write(
    client: AsyncClient, admin: dict[str, str], install_runtime
) -> None:
    await client.post(
        f"{API}/users",
        headers=admin,
        json={
            "username": "agent-operator",
            "password": "operator-pass-123",
            "role_names": ["operator"],
        },
    )
    login = await client.post(
        f"{API}/auth/login",
        json={"username": "agent-operator", "password": "operator-pass-123"},
    )
    operator = {"Authorization": f"Bearer {login.json()['access_token']}"}
    model = FakeModel([{"content": "operator answer"}])
    tools = FakeTools()
    snapshot = make_snapshot(
        agent_id="agent-operator", tool_names=[], system_prompt="Operator test agent"
    )
    install_runtime(model=model, tools=tools, snapshot=snapshot)
    response = await client.post(
        f"{API}/executions",
        headers=operator,
        json={"agent_id": "agent-operator", "input": "hello"},
    )
    assert response.status_code == 201, response.text
    await _wait_for_status(client, operator, response.json()["id"], {"completed"})
