from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from fastapi import Depends
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.contracts import (
    EnvironmentDescriptor,
    EnvironmentResource,
    ToolContext,
    UserContext,
)
from app.core.exceptions import NotFoundError, PermissionDeniedError, ValidationError
from app.infrastructure.database.session import get_db
from app.main import app
from app.modules.execution.environments import ArtifactWorkspace
from app.modules.execution.router import get_execution_service
from app.modules.execution.service import _TASKS, ExecutionService
from app.modules.permission.service import PermissionService
from app.modules.runtime.checkpoint import close_checkpointer
from app.modules.runtime.service import RuntimeService
from app.modules.tool.builtin.artifacts import read_artifact_tool
from tests.test_runtime import (
    USER,
    FakeLoader,
    FakeModel,
    FakeSink,
    FakeTools,
    checkpointer,  # noqa: F401 - pytest fixture
    make_snapshot,
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


def _install_runtime() -> None:
    async def _dependency(db: AsyncSession = Depends(get_db)) -> ExecutionService:
        return ExecutionService(
            db,
            agent_loader=FakeLoader(make_snapshot()),
            tool_gateway=FakeTools(),
            model_gateway=FakeModel([{"content": "done"}]),
        )

    app.dependency_overrides[get_execution_service] = _dependency


async def _admin_actor(db: AsyncSession) -> UserContext:
    permissions = PermissionService(db)
    user = await permissions.repo.get_user_by_username("admin")
    assert user is not None
    return await permissions.load_context(user.id)


async def _upload(client: AsyncClient, admin: dict[str, str], name: str, body: bytes) -> str:
    response = await client.post(
        f"{API}/artifacts",
        headers=admin,
        files={"file": (name, body, "text/plain")},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _execution_row(
    db: AsyncSession, actor: UserContext, artifact_ids: list[str]
) -> str:
    """Create the execution row without scheduling a background run."""

    from app.core.ids import new_id
    from app.modules.execution.models import AgentExecution
    from app.modules.execution.repository import ExecutionRepository

    execution = AgentExecution(
        id=new_id(),
        agent_id="agent-1",
        user_id=actor.user_id,
        status="running",
        input_text="读附件",
        input_payload={"input": "读附件", "artifact_ids": list(artifact_ids)},
    )
    await ExecutionRepository(db).create_execution(execution)
    await db.commit()
    return execution.id


async def test_workspace_declares_only_attached_artifacts(
    client: AsyncClient, admin: dict[str, str], db: AsyncSession
) -> None:
    attached = await _upload(client, admin, "attached.txt", b"hello workspace")
    other = await _upload(client, admin, "other.txt", b"not mine")
    actor = await _admin_actor(db)
    execution_id = await _execution_row(db, actor, [attached])

    workspace = ArtifactWorkspace(db)
    descriptor = await workspace.open(execution_id=execution_id, user=actor)

    assert descriptor.kind == "artifact_workspace"
    assert [item.id for item in descriptor.resources] == [attached]
    assert descriptor.constraints["read_only"] is True

    resource, data = await workspace.read(
        execution_id=execution_id, user=actor, resource_id=attached
    )
    assert resource.name == "attached.txt"
    assert data == b"hello workspace"

    with pytest.raises(NotFoundError):
        await workspace.read(execution_id=execution_id, user=actor, resource_id=other)


async def test_workspace_rejects_other_users(
    client: AsyncClient, admin: dict[str, str], db: AsyncSession
) -> None:
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
    owner = await _admin_actor(db)
    execution_id = await _execution_row(db, owner, [])

    with pytest.raises(PermissionDeniedError):
        await ArtifactWorkspace(db).open(execution_id=execution_id, user=viewer)


async def test_workspace_enforces_read_cap(
    client: AsyncClient,
    admin: dict[str, str],
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_id = await _upload(client, admin, "big.txt", b"x" * 200)
    actor = await _admin_actor(db)
    execution_id = await _execution_row(db, actor, [artifact_id])

    monkeypatch.setattr(get_settings(), "environment_max_resource_bytes", 100)
    with pytest.raises(ValidationError, match="read limit"):
        await ArtifactWorkspace(db).read(
            execution_id=execution_id, user=actor, resource_id=artifact_id
        )


async def test_read_artifact_tool_uses_workspace_scope(
    client: AsyncClient, admin: dict[str, str], db: AsyncSession
) -> None:
    attached = await _upload(client, admin, "attached.txt", b"visible")
    outside = await _upload(client, admin, "outside.txt", b"hidden")
    actor = await _admin_actor(db)
    execution_id = await _execution_row(db, actor, [attached])
    ctx = ToolContext(
        user=actor, tool_name="platform_read_artifact", execution_id=execution_id
    )

    output = await read_artifact_tool({"artifact_id": attached}, ctx)
    assert output["filename"] == "attached.txt"
    assert output["content"] == "visible"

    with pytest.raises(NotFoundError):
        await read_artifact_tool({"artifact_id": outside}, ctx)


class FakeEnvironmentGateway:
    def __init__(self, descriptor: EnvironmentDescriptor) -> None:
        self.descriptor = descriptor
        self.opened: list[str] = []
        self.closed: list[str] = []

    async def open(self, *, execution_id: str, user: UserContext) -> EnvironmentDescriptor:
        self.opened.append(execution_id)
        return self.descriptor

    async def close(self, environment_id: str) -> None:
        self.closed.append(environment_id)


def _descriptor() -> EnvironmentDescriptor:
    return EnvironmentDescriptor(
        kind="artifact_workspace",
        environment_id="exec-env",
        label="conversation c-1",
        resources=[
            EnvironmentResource(
                id="a-1", name="rows.csv", content_type="text/csv", size=42
            )
        ],
        constraints={"read_only": True},
    )


async def test_runtime_injects_environment_and_closes_it(checkpointer) -> None:  # noqa: F811
    sink = FakeSink()
    gateway = FakeEnvironmentGateway(_descriptor())
    model = FakeModel([{"content": "done"}])
    runtime = RuntimeService(
        model_gateway=model,
        tool_gateway=FakeTools(),
        sink=sink,
        checkpointer=checkpointer,
        environment_gateway=gateway,
    )

    result = await runtime.run(
        execution_id="exec-env",
        snapshot=make_snapshot(),
        user=USER,
        user_text="read the csv",
    )

    assert result.status == "completed"
    system = model.calls[0]["messages"][0]
    assert "## Environment (workspace)" in system.content
    assert "rows.csv" in system.content
    assert "a-1" in system.content
    assert "environment_opened" in sink.types()
    assert gateway.closed == ["exec-env"]


async def test_environment_stays_open_while_waiting_for_approval(
    checkpointer,  # noqa: F811 - pytest fixture
) -> None:
    sink = FakeSink()
    gateway = FakeEnvironmentGateway(_descriptor())
    model = FakeModel(
        [{"tool_calls": [{"name": "wire", "arguments": {"amount": 5}}]}]
    )
    tools = FakeTools([_high_risk_tool()])
    runtime = RuntimeService(
        model_gateway=model,
        tool_gateway=tools,
        sink=sink,
        checkpointer=checkpointer,
        environment_gateway=gateway,
    )

    result = await runtime.run(
        execution_id="exec-env-paused",
        snapshot=make_snapshot(tool_names=["wire"]),
        user=USER,
        user_text="send money",
    )

    assert result.status == "waiting_approval"
    assert gateway.closed == []


def _high_risk_tool() -> Any:
    from tests.test_runtime import make_tool

    return make_tool("wire", "high", requires_approval=True)
