from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from fastapi import Depends
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.session import get_db
from app.main import app
from app.modules.execution.router import get_execution_service
from app.modules.execution.service import _TASKS, ExecutionService
from app.modules.runtime.checkpoint import close_checkpointer
from tests.test_runtime import FakeLoader, FakeModel, FakeTools, make_snapshot

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


def _install_runtime(*, model: Any = None, tools: Any = None) -> None:
    model = model or FakeModel([{"content": "done"}])
    tools = tools or FakeTools()

    async def _dependency(db: AsyncSession = Depends(get_db)) -> ExecutionService:
        return ExecutionService(
            db,
            agent_loader=FakeLoader(make_snapshot()),
            tool_gateway=tools,
            model_gateway=model,
        )

    app.dependency_overrides[get_execution_service] = _dependency


async def _wait_for(client: AsyncClient, headers: dict[str, str], execution_id: str) -> dict:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 15
    detail: dict[str, Any] = {}
    while loop.time() < deadline:
        response = await client.get(f"{API}/executions/{execution_id}", headers=headers)
        assert response.status_code == 200, response.text
        detail = response.json()
        if detail["status"] in {"completed", "incomplete", "failed", "cancelled"}:
            return detail
        await asyncio.sleep(0.02)
    raise AssertionError(f"execution did not finish: {detail}")


async def test_execution_creates_a_task_with_goal(
    client: AsyncClient, admin: dict[str, str]
) -> None:
    _install_runtime()
    created = await client.post(
        f"{API}/executions",
        headers=admin,
        json={"agent_id": "agent-1", "input": "检查这批交易的重复付款"},
    )
    assert created.status_code == 201, created.text
    execution = created.json()
    assert execution["task_id"]

    detail = await _wait_for(client, admin, execution["id"])
    assert detail["status"] == "completed"
    assert detail["task_id"] == execution["task_id"]
    assert detail["steps"] == []

    task = await client.get(f"{API}/tasks/{execution['task_id']}", headers=admin)
    assert task.status_code == 200, task.text
    body = task.json()
    assert body["goal"] == "检查这批交易的重复付款"
    assert body["status"] == "active"
    assert body["steps"] == []

    listing = await client.get(
        f"{API}/tasks?conversation_id={execution['conversation_id']}", headers=admin
    )
    assert listing.status_code == 200, listing.text
    assert [row["id"] for row in listing.json()] == [execution["task_id"]]


async def test_plan_tool_persists_steps_on_the_task(
    client: AsyncClient, admin: dict[str, str]
) -> None:
    suffix = uuid.uuid4().hex[:8]
    plan = [
        {"id": "1", "title": "读取附件", "status": "in_progress"},
        {"id": "2", "title": "汇总并回答", "status": "pending"},
    ]
    provider = await client.post(
        f"{API}/model-providers",
        headers=admin,
        json={"name": f"task-provider-{suffix}", "provider_type": "mock"},
    )
    assert provider.status_code == 201, provider.text
    config = await client.post(
        f"{API}/model-configs",
        headers=admin,
        json={
            "name": f"task-config-{suffix}",
            "provider_id": provider.json()["id"],
            "model_name": "mock-agent",
            "extra": {
                "script": [
                    {"tool_calls": [{"name": "platform_update_plan", "arguments": {"plan": plan}}]},
                    {"content": "计划已建立"},
                ]
            },
        },
    )
    assert config.status_code == 201, config.text
    prompt = await client.post(
        f"{API}/prompts", headers=admin, json={"name": f"task-prompt-{suffix}"}
    )
    assert prompt.status_code == 201, prompt.text
    version = await client.post(
        f"{API}/prompts/{prompt.json()['id']}/versions",
        headers=admin,
        json={"system_prompt": "你是任务测试助手", "publish": True},
    )
    assert version.status_code == 201, version.text
    agent = await client.post(
        f"{API}/agents",
        headers=admin,
        json={
            "name": f"task-agent-{suffix}",
            "config": {
                "model": {"config_id": config.json()["id"]},
                "prompt": {
                    "prompt_id": prompt.json()["id"],
                    "version_id": version.json()["id"],
                },
                "tools": ["platform_update_plan"],
                "retrieval": {"enabled": False},
            },
        },
    )
    assert agent.status_code == 201, agent.text
    published = await client.post(
        f"{API}/agents/{agent.json()['id']}/publish", headers=admin
    )
    assert published.status_code == 200, published.text

    created = await client.post(
        f"{API}/executions",
        headers=admin,
        json={"agent_id": agent.json()["id"], "input": "两步任务"},
    )
    assert created.status_code == 201, created.text
    execution = created.json()
    detail = await _wait_for(client, admin, execution["id"])
    assert detail["status"] == "completed", detail
    assert detail["output_text"] == "计划已建立"

    task = await client.get(f"{API}/tasks/{execution['task_id']}", headers=admin)
    assert task.status_code == 200, task.text
    steps = task.json()["steps"]
    assert [(step["step_key"], step["title"], step["status"]) for step in steps] == [
        ("1", "读取附件", "in_progress"),
        ("2", "汇总并回答", "pending"),
    ]
    assert detail["steps"] == steps


async def test_task_is_not_readable_by_other_users(
    client: AsyncClient, admin: dict[str, str]
) -> None:
    _install_runtime()
    created = await client.post(
        f"{API}/executions", headers=admin, json={"agent_id": "agent-1", "input": "私有任务"}
    )
    task_id = created.json()["task_id"]

    username = f"viewer_{uuid.uuid4().hex[:8]}"
    registered = await client.post(
        f"{API}/users",
        headers=admin,
        json={"username": username, "password": "viewer-pass-123", "role_names": ["viewer"]},
    )
    assert registered.status_code == 201, registered.text
    login = await client.post(
        f"{API}/auth/login", json={"username": username, "password": "viewer-pass-123"}
    )
    assert login.status_code == 200, login.text
    viewer = {"Authorization": f"Bearer {login.json()['access_token']}"}

    response = await client.get(f"{API}/tasks/{task_id}", headers=viewer)
    assert response.status_code == 403, response.text


async def test_apply_plan_is_idempotent_and_prunes_removed_steps(
    client: AsyncClient, admin: dict[str, str], db: AsyncSession
) -> None:
    from app.core.exceptions import ValidationError
    from app.modules.execution.tasks import TaskService
    from app.modules.permission.service import PermissionService

    permissions = PermissionService(db)
    user = await permissions.repo.get_user_by_username("admin")
    assert user is not None
    actor = await permissions.load_context(user.id)

    tasks = TaskService(db)
    task = await tasks.ensure_task(
        agent_id="agent-1", user_id=actor.user_id, conversation_id=None, goal="g"
    )
    plan = [
        {"id": "1", "title": "第一步", "status": "in_progress"},
        {"id": "2", "title": "第二步", "status": "pending"},
    ]
    first = await tasks.apply_plan_to_task(task.id, plan)
    assert [step.step_key for step in first] == ["1", "2"]

    second = await tasks.apply_plan_to_task(
        task.id, [{"id": "1", "title": "第一步", "status": "done"}]
    )
    assert len(second) == 1
    assert second[0].status == "done"
    assert second[0].result is None

    with pytest.raises(ValidationError):
        await tasks.apply_plan_to_task(
            task.id, [{"id": "x", "title": "", "status": "pending"}]
        )
