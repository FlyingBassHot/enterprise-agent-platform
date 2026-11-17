"""Fault-scenario contracts.

These encode how the harness must behave when things go wrong — the behaviour a
functional test suite usually misses.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from fastapi import Depends
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.contracts import ToolContext, ToolDescriptor, ToolInvocation, UserContext
from app.core.enums import ExecutionStatus
from app.core.exceptions import PermissionDeniedError
from app.infrastructure.database.session import get_db
from app.main import app
from app.modules.execution.artifacts import read_artifact_for_tool
from app.modules.execution.models import AgentExecution
from app.modules.execution.repository import ExecutionRepository
from app.modules.execution.router import get_execution_service
from app.modules.execution.service import _TASKS, ExecutionService
from app.modules.permission.service import PermissionService
from app.modules.runtime.checkpoint import close_checkpointer
from app.modules.runtime.service import RuntimeService
from app.modules.tool.registry import native_registry
from app.modules.tool.service import ToolService
from tests.test_runtime import (
    USER,
    FakeLoader,
    FakeModel,
    FakeSink,
    FakeTools,
    checkpointer,  # noqa: F401 - pytest fixture
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


async def _wait_for_status(
    client: AsyncClient, headers: dict[str, str], execution_id: str, statuses: set[str]
) -> dict:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 15
    detail: dict[str, Any] = {}
    while loop.time() < deadline:
        response = await client.get(f"{API}/executions/{execution_id}", headers=headers)
        assert response.status_code == 200, response.text
        detail = response.json()
        if detail["status"] in statuses:
            return detail
        await asyncio.sleep(0.02)
    raise AssertionError(f"execution never reached {statuses}: {detail}")


async def test_identical_tool_calls_stop_for_no_progress(checkpointer) -> None:  # noqa: F811
    sink = FakeSink()
    tools = FakeTools([make_tool("echo", "low")])
    # The script repeats the same call on every turn.
    model = FakeModel([{"tool_calls": [{"name": "echo", "arguments": {"value": 1}}]}])
    runtime = RuntimeService(
        model_gateway=model,
        tool_gateway=tools,
        sink=sink,
        checkpointer=checkpointer,
    )

    result = await runtime.run(
        execution_id="exec-no-progress",
        snapshot=make_snapshot(tool_names=["echo"], max_iterations=8),
        user=USER,
        user_text="loop forever",
    )

    assert len(tools.invocations) == 2
    assert result.status == "failed"
    assert result.error is not None and "no_progress" in result.error
    assert result.output_payload["stop_reason"] == "no_progress"
    assert "no_progress_detected" in sink.types()
    denied = [
        event
        for event in sink.events
        if event["type"] == "tool_call_completed"
        and event["payload"].get("error_code") == "repeated_call"
    ]
    assert denied


async def test_invalid_arguments_never_reach_the_handler(
    client, db
) -> None:  # noqa: ARG001
    name = f"arg_probe_{uuid.uuid4().hex[:8]}"
    calls: list[dict[str, Any]] = []

    async def handler(arguments: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        calls.append(arguments)
        return {"ok": True}

    native_registry.register(
        ToolDescriptor(
            name=name,
            description="probe",
            input_schema={
                "type": "object",
                "properties": {"n": {"type": "integer"}},
                "required": ["n"],
                "additionalProperties": False,
            },
            risk_level="low",
            timeout_seconds=5,
        ),
        handler,
    )
    try:
        await ToolService(db).sync_native()
        actor = UserContext(
            user_id="u-invalid", username="tester", permissions={"tools:execute"}
        )
        result = await ToolService(db).execute(
            ToolInvocation(tool_name=name, arguments={"n": "not-an-int"}, user_id="u-invalid"),
            actor,
        )
        assert result.status == "failed"
        assert calls == []
        assert result.error and "n" in result.error
    finally:
        native_registry.unregister(name)


async def test_stale_execution_is_marked_resumable(client, db) -> None:  # noqa: ARG001
    repo = ExecutionRepository(db)
    execution = await repo.create_execution(
        AgentExecution(
            id=f"stale-{uuid.uuid4().hex[:8]}",
            agent_id="agent-1",
            user_id="u-stale",
            status=ExecutionStatus.RUNNING.value,
            input_text="interrupted",
        )
    )
    await db.commit()

    recovered = await ExecutionService(db).recover_stale_executions()
    assert recovered >= 1

    await db.commit()
    stored = await repo.get_execution(execution.id)
    assert stored is not None
    assert stored.status == ExecutionStatus.FAILED.value
    assert "resumable" in (stored.error or "")

    events = await repo.list_events(execution.id)
    assert any(
        event.type == "execution_failed"
        and (event.payload or {}).get("recoverable") is True
        for event in events
    )


async def test_cancel_at_approval_leaves_no_side_effects(
    client: AsyncClient, admin: dict[str, str]
) -> None:
    tools = FakeTools([make_tool("wire", "high")])
    model = FakeModel([{"tool_calls": [{"name": "wire", "arguments": {"amount": 1}}]}])

    async def _dependency(db: AsyncSession = Depends(get_db)) -> ExecutionService:
        return ExecutionService(
            db,
            agent_loader=FakeLoader(make_snapshot(tool_names=["wire"])),
            tool_gateway=tools,
            model_gateway=model,
        )

    app.dependency_overrides[get_execution_service] = _dependency

    created = await client.post(
        f"{API}/executions", headers=admin, json={"agent_id": "agent-1", "input": "pay"}
    )
    assert created.status_code == 201, created.text
    execution = created.json()
    paused = await _wait_for_status(client, admin, execution["id"], {"waiting_approval"})
    assert paused["task_id"]

    cancelled = await client.post(
        f"{API}/executions/{execution['id']}/cancel", headers=admin
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    assert tools.invocations == []

    # Changing the goal is modelled as a new execution on the same task.
    continued = await client.post(
        f"{API}/executions",
        headers=admin,
        json={
            "agent_id": "agent-1",
            "input": "改为先确认金额",
            "task_id": execution["task_id"],
        },
    )
    assert continued.status_code == 201, continued.text
    assert continued.json()["task_id"] == execution["task_id"]
    await _wait_for_status(
        client, admin, continued.json()["id"], {"waiting_approval"}
    )
    await client.post(f"{API}/executions/{continued.json()['id']}/cancel", headers=admin)


class BigOutputTools(FakeTools):
    async def execute(
        self, invocation: ToolInvocation, user: UserContext
    ) -> Any:
        from app.core.contracts import ToolExecutionResult

        self.invocations.append(invocation)
        return ToolExecutionResult(
            ok=True, status="success", output={"payload": "x" * 3000}, duration_ms=1
        )


async def test_context_budget_bounds_prompt_growth(
    monkeypatch: pytest.MonkeyPatch,
    checkpointer,  # noqa: F811 - pytest fixture
) -> None:
    from app.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "context_char_budget", 5000)
    monkeypatch.setattr(settings, "context_keep_recent_messages", 2)

    script = [
        {"tool_calls": [{"name": "echo", "arguments": {"n": index}}]}
        for index in range(6)
    ] + [{"content": "done"}]
    sink = FakeSink()
    tools = BigOutputTools([make_tool("echo", "low")])
    model = FakeModel(script)
    runtime = RuntimeService(
        model_gateway=model,
        tool_gateway=tools,
        sink=sink,
        checkpointer=checkpointer,
    )

    result = await runtime.run(
        execution_id="exec-context-budget",
        snapshot=make_snapshot(tool_names=["echo"], max_iterations=10),
        user=USER,
        user_text="go",
    )

    assert result.status == "completed"
    assert "context_compacted" in sink.types()
    sizes = [
        sum(len(message.content) for message in call["messages"])
        for call in model.calls
    ]
    # Newest tool result plus the fixed prompt may exceed the budget slightly,
    # but growth must stay bounded instead of accumulating every observation.
    assert max(sizes) <= 5000 + 4000


async def test_tool_result_replay_does_not_repeat_side_effects(
    client, db
) -> None:  # noqa: ARG001
    name = f"replay_probe_{uuid.uuid4().hex[:8]}"
    calls: list[dict[str, Any]] = []

    async def handler(arguments: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        calls.append(arguments)
        return {"echo": arguments}

    native_registry.register(
        ToolDescriptor(
            name=name,
            description="probe",
            input_schema={"type": "object", "properties": {}},
            risk_level="low",
            timeout_seconds=5,
        ),
        handler,
    )
    try:
        await ToolService(db).sync_native()
        actor = UserContext(
            user_id="u-replay", username="tester", permissions={"tools:execute"}
        )
        invocation = ToolInvocation(
            tool_name=name,
            arguments={"value": 1},
            user_id="u-replay",
            execution_id="exec-replay",
            tool_call_id="call-1",
        )
        first = await ToolService(db).execute(invocation, actor)
        second = await ToolService(db).execute(invocation, actor)

        assert calls == [{"value": 1}]
        assert first.replayed is False
        assert second.replayed is True
        assert second.output == first.output
    finally:
        native_registry.unregister(name)


async def test_unauthorized_artifact_read_is_denied(
    client: AsyncClient, admin: dict[str, str], db: AsyncSession
) -> None:
    upload = await client.post(
        f"{API}/artifacts",
        headers=admin,
        files={"file": ("secret.md", b"# private", "text/markdown")},
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
    user = await permissions.repo.get_user_by_username(username)
    assert user is not None
    viewer = await permissions.load_context(user.id)

    with pytest.raises(PermissionDeniedError):
        await read_artifact_for_tool(upload.json()["id"], viewer)

    # The tool surfaces the denial without leaking content.
    from app.modules.tool.builtin.artifacts import read_artifact_tool

    with pytest.raises(PermissionDeniedError):
        await read_artifact_tool(
            {"artifact_id": upload.json()["id"]},
            ToolContext(user=viewer, tool_name="platform_read_artifact"),
        )


async def test_replaying_a_failed_call_does_not_break_uniqueness(client, db) -> None:  # noqa: ARG001
    name = f"fail_replay_{uuid.uuid4().hex[:8]}"
    calls: list[str] = []

    async def handler(arguments: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        calls.append("run")
        raise RuntimeError("handler exploded")

    native_registry.register(
        ToolDescriptor(
            name=name,
            description="probe",
            input_schema={"type": "object", "properties": {}},
            risk_level="low",
            timeout_seconds=5,
        ),
        handler,
    )
    try:
        await ToolService(db).sync_native()
        actor = UserContext(
            user_id="u-fail-replay", username="tester", permissions={"tools:execute"}
        )
        invocation = ToolInvocation(
            tool_name=name,
            arguments={},
            user_id=actor.user_id,
            execution_id="exec-fail-replay",
            tool_call_id="c1",
        )
        first = await ToolService(db).execute(invocation, actor)
        second = await ToolService(db).execute(invocation, actor)

        assert first.status == "failed"
        assert second.status == "failed"
        assert second.replayed is True
        assert calls == ["run"]

        # A reused id with different arguments must not insert a second row.
        mismatched = invocation.model_copy(update={"arguments": {"value": 1}})
        third = await ToolService(db).execute(mismatched, actor)
        assert third.ok is False
        assert "reused" in (third.error or "")
        assert calls == ["run"]

        # The session stays usable after these paths.
        other = invocation.model_copy(update={"tool_call_id": "c2"})
        fourth = await ToolService(db).execute(other, actor)
        assert fourth.status == "failed"
        assert calls == ["run", "run"]
    finally:
        native_registry.unregister(name)
