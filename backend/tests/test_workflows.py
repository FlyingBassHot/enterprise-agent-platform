from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.contracts import UserContext
from app.core.exceptions import NotFoundError, PermissionDeniedError
from app.modules.permission.service import PermissionService
from app.modules.workflow.exceptions import (
    WorkflowDisabledError,
    WorkflowNotPublishedError,
)
from app.modules.workflow.models import (  # noqa: F401  (create_all)
    Workflow,
    WorkflowExecution,
    WorkflowVersion,
)
from app.modules.workflow.service import WorkflowService

WORKFLOWS = "/api/v1/workflows"
AGENTS = "/api/v1/agents"
PROMPTS = "/api/v1/prompts"
PASSWORD = "workflow-pass-123"


def unique(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:10]}"


def simple_definition(*, name: str = "end") -> dict:
    return {
        "version": 1,
        "nodes": [
            {"id": "start", "type": "start", "next": name},
            {"id": name, "type": "end"},
        ],
    }


def llm_definition(*, model_config_id: str | None = None) -> dict:
    config: dict = {"prompt": "Summarize the input"}
    if model_config_id is not None:
        config["model_config_id"] = model_config_id
    return {
        "version": 1,
        "nodes": [
            {"id": "start", "type": "start", "next": "llm"},
            {"id": "llm", "type": "llm", "config": config, "next": "end"},
            {"id": "end", "type": "end"},
        ],
    }


def tool_definition(tool_name: str) -> dict:
    return {
        "version": 1,
        "nodes": [
            {"id": "start", "type": "start", "next": "tool"},
            {
                "id": "tool",
                "type": "tool",
                "config": {"tool_name": tool_name, "arguments": {}},
                "next": "end",
            },
            {"id": "end", "type": "end"},
        ],
    }


def agent_definition(agent_id: str) -> dict:
    return {
        "version": 1,
        "nodes": [
            {"id": "start", "type": "start", "next": "agent"},
            {
                "id": "agent",
                "type": "agent",
                "config": {"agent_id": agent_id},
                "next": "end",
            },
            {"id": "end", "type": "end"},
        ],
    }


def knowledge_definition(base_ids: list[str]) -> dict:
    return {
        "version": 1,
        "nodes": [
            {"id": "start", "type": "start", "next": "knowledge"},
            {
                "id": "knowledge",
                "type": "knowledge",
                "config": {"knowledge_base_ids": base_ids, "query": "policy"},
                "next": "end",
            },
            {"id": "end", "type": "end"},
        ],
    }


async def login(client: AsyncClient, username: str) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login", json={"username": username, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def create_user(
    client: AsyncClient,
    admin: dict[str, str],
    *,
    role: str = "developer",
) -> tuple[str, dict[str, str]]:
    username = unique(f"user-{role}")
    created = await client.post(
        "/api/v1/users",
        headers=admin,
        json={
            "username": username,
            "password": PASSWORD,
            "display_name": username,
            "role_names": [role],
        },
    )
    assert created.status_code == 201, created.text
    return created.json()["id"], await login(client, username)


async def create_workflow(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    definition: dict | None = None,
) -> dict:
    response = await client.post(
        WORKFLOWS,
        headers=headers,
        json={
            "name": unique("workflow"),
            "description": "workflow under test",
            "definition": definition or simple_definition(),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def create_model_config(
    client: AsyncClient, admin: dict[str, str]
) -> str:
    provider = await client.post(
        "/api/v1/model-providers",
        headers=admin,
        json={"name": unique("provider"), "provider_type": "mock"},
    )
    assert provider.status_code == 201, provider.text
    config = await client.post(
        "/api/v1/model-configs",
        headers=admin,
        json={
            "name": unique("config"),
            "provider_id": provider.json()["id"],
            "model_name": "mock-agent",
        },
    )
    assert config.status_code == 201, config.text
    return config.json()["id"]


async def create_prompt(client: AsyncClient, admin: dict[str, str]) -> str:
    prompt = await client.post(
        PROMPTS, headers=admin, json={"name": unique("prompt")}
    )
    assert prompt.status_code == 201, prompt.text
    version = await client.post(
        f"{PROMPTS}/{prompt.json()['id']}/versions",
        headers=admin,
        json={
            "system_prompt": "You are a workflow test assistant.",
            "task_prompt": "Answer.",
            "variables": [],
        },
    )
    assert version.status_code == 201, version.text
    return prompt.json()["id"]


async def create_agent(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    config_id: str,
    prompt_id: str,
    tools: list[str] | None = None,
    mode: str = "agent",
    workflow_id: str | None = None,
    publish: bool = True,
) -> dict:
    execution: dict = {"mode": mode}
    if workflow_id is not None:
        execution["workflow_id"] = workflow_id
    response = await client.post(
        AGENTS,
        headers=headers,
        json={
            "name": unique("agent"),
            "config": {
                "model": {"config_id": config_id},
                "prompt": {"prompt_id": prompt_id},
                "tools": tools or [],
                "knowledge_base_ids": [],
                "retrieval": {"enabled": False},
                "execution": execution,
            },
        },
    )
    assert response.status_code == 201, response.text
    agent = response.json()
    if publish:
        published = await client.post(f"{AGENTS}/{agent['id']}/publish", headers=headers)
        assert published.status_code == 200, published.text
    return agent


async def create_http_tool(
    client: AsyncClient,
    admin: dict[str, str],
    *,
    risk_level: str,
    requires_approval: bool = False,
) -> str:
    response = await client.post(
        "/api/v1/tools",
        headers=admin,
        json={
            "name": unique("http-tool"),
            "description": "workflow test tool",
            "source_type": "http",
            "risk_level": risk_level,
            "requires_approval": requires_approval,
            "config": {"method": "GET", "url": "https://example.invalid/hook"},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["name"]


async def validate(
    client: AsyncClient, headers: dict[str, str], workflow_id: str
) -> dict:
    response = await client.post(f"{WORKFLOWS}/{workflow_id}/validate", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def actor_for(db: AsyncSession, user_id: str) -> UserContext:
    return await PermissionService(db).load_context(user_id)


async def test_workflow_crud_and_graph_validation(
    client: AsyncClient, admin: dict[str, str]
) -> None:
    created = await create_workflow(client, admin)
    workflow_id = created["id"]
    assert created["status"] == "draft"
    assert created["current_version"] == 0
    assert created["current_version_id"] is None
    assert created["published_at"] is None
    assert created["definition"]["nodes"][0]["id"] == "start"

    listing = await client.get(WORKFLOWS, headers=admin)
    assert listing.status_code == 200
    assert any(item["id"] == workflow_id for item in listing.json())

    fetched = await client.get(f"{WORKFLOWS}/{workflow_id}", headers=admin)
    assert fetched.status_code == 200
    assert fetched.json()["name"] == created["name"]

    duplicate = await client.post(
        WORKFLOWS,
        headers=admin,
        json={
            "name": created["name"],
            "definition": simple_definition(),
        },
    )
    assert duplicate.status_code == 409, duplicate.text

    updated = await client.put(
        f"{WORKFLOWS}/{workflow_id}",
        headers=admin,
        json={
            "description": "updated",
            "definition": llm_definition(),
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["description"] == "updated"
    assert updated.json()["definition"]["nodes"][1]["type"] == "llm"

    valid = await validate(client, admin, workflow_id)
    assert valid["valid"] is True, valid

    # Schema-valid but semantically broken graph: unreachable node.
    broken = await create_workflow(
        client,
        admin,
        definition={
            "nodes": [
                {"id": "start", "type": "start", "next": "end"},
                {"id": "end", "type": "end"},
                {
                    "id": "orphan",
                    "type": "llm",
                    "config": {"prompt": "unused"},
                    "next": "end",
                },
            ]
        },
    )
    broken_validation = await validate(client, admin, broken["id"])
    assert broken_validation["valid"] is False
    assert any("unreachable" in error for error in broken_validation["errors"])
    broken_publish = await client.post(
        f"{WORKFLOWS}/{broken['id']}/publish", headers=admin
    )
    assert broken_publish.status_code == 422, broken_publish.text

    deleted = await client.delete(f"{WORKFLOWS}/{workflow_id}", headers=admin)
    assert deleted.status_code == 204
    missing = await client.get(f"{WORKFLOWS}/{workflow_id}", headers=admin)
    assert missing.status_code == 404

    # Invalid definitions are rejected by the request schema.
    malformed = await client.post(
        WORKFLOWS,
        headers=admin,
        json={
            "name": unique("bad"),
            "definition": {"nodes": [{"id": "only", "type": "start"}]},
        },
    )
    assert malformed.status_code == 422


async def test_publish_versions_and_snapshot_pinning(
    client: AsyncClient, admin: dict[str, str], db: AsyncSession
) -> None:
    created = await create_workflow(client, admin)
    workflow_id = created["id"]

    published = await client.post(f"{WORKFLOWS}/{workflow_id}/publish", headers=admin)
    assert published.status_code == 200, published.text
    body = published.json()
    assert body["status"] == "published"
    assert body["current_version"] == 1
    assert body["current_version_id"]
    assert body["published_at"] is not None

    versions = await client.get(f"{WORKFLOWS}/{workflow_id}/versions", headers=admin)
    assert versions.status_code == 200
    first = versions.json()[0]
    assert first["version"] == 1
    assert first["definition"]["nodes"][0]["id"] == "start"
    assert first["created_by"]

    snapshot = await WorkflowService(db).load_published(workflow_id)
    assert snapshot.workflow_id == workflow_id
    assert snapshot.version == 1
    assert snapshot.version_id == first["id"]
    assert [node.id for node in snapshot.definition.nodes] == ["start", "end"]

    # The draft moves on, the published version stays immutable.
    updated = await client.put(
        f"{WORKFLOWS}/{workflow_id}",
        headers=admin,
        json={"definition": llm_definition()},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["current_version"] == 1
    unchanged = (await client.get(f"{WORKFLOWS}/{workflow_id}/versions", headers=admin)).json()
    assert unchanged[0] == first
    pinned = await WorkflowService(db).load_published(workflow_id)
    assert [node.id for node in pinned.definition.nodes] == ["start", "end"]

    second = await client.post(f"{WORKFLOWS}/{workflow_id}/publish", headers=admin)
    assert second.status_code == 200, second.text
    assert second.json()["current_version"] == 2
    versions = (await client.get(f"{WORKFLOWS}/{workflow_id}/versions", headers=admin)).json()
    assert [item["version"] for item in versions] == [1, 2]
    first_snapshot = await WorkflowService(db).load_version(workflow_id, versions[0]["id"])
    second_snapshot = await WorkflowService(db).load_version(workflow_id, versions[1]["id"])
    assert [node.id for node in first_snapshot.definition.nodes] == ["start", "end"]
    assert [node.id for node in second_snapshot.definition.nodes] == ["start", "llm", "end"]

    execution_id = await WorkflowService(db).record_execution(
        workflow_id=workflow_id,
        workflow_version_id=versions[1]["id"],
        user_id=snapshot.owner_id,
        status="succeeded",
        input={"query": "hello"},
        output={"answer": "hi"},
    )
    row = await db.get(WorkflowExecution, execution_id)
    assert row is not None
    assert row.workflow_id == workflow_id
    assert row.input == {"query": "hello"}
    assert row.finished_at is not None

    published_delete = await client.delete(f"{WORKFLOWS}/{workflow_id}", headers=admin)
    assert published_delete.status_code == 409, published_delete.text

    disabled = await client.post(f"{WORKFLOWS}/{workflow_id}/disable", headers=admin)
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["status"] == "disabled"
    with pytest.raises(WorkflowDisabledError):
        await WorkflowService(db).load_published(workflow_id)

    # Publishing a disabled workflow re-enables it with a new version.
    reenabled = await client.post(f"{WORKFLOWS}/{workflow_id}/publish", headers=admin)
    assert reenabled.status_code == 200, reenabled.text
    assert reenabled.json()["status"] == "published"
    assert reenabled.json()["current_version"] == 3

    await client.post(f"{WORKFLOWS}/{workflow_id}/disable", headers=admin)
    deleted = await client.delete(f"{WORKFLOWS}/{workflow_id}", headers=admin)
    assert deleted.status_code == 204
    remaining = await db.scalars(
        select(WorkflowVersion).where(WorkflowVersion.workflow_id == workflow_id)
    )
    assert remaining.all() == []
    with pytest.raises(NotFoundError):
        await WorkflowService(db).load_published(workflow_id)


async def test_validation_resolves_tool_knowledge_and_llm_bindings(
    client: AsyncClient, admin: dict[str, str]
) -> None:
    tool_workflow = await create_workflow(
        client, admin, definition=tool_definition("analyze_transactions")
    )
    assert (await validate(client, admin, tool_workflow["id"]))["valid"] is True

    unknown_tool = await create_workflow(
        client, admin, definition=tool_definition("tool-that-does-not-exist")
    )
    result = await validate(client, admin, unknown_tool["id"])
    assert result["valid"] is False
    assert any("tool-that-does-not-exist" in error for error in result["errors"])
    publish = await client.post(
        f"{WORKFLOWS}/{unknown_tool['id']}/publish", headers=admin
    )
    assert publish.status_code == 422, publish.text

    knowledge_base = await client.post(
        "/api/v1/knowledge-bases",
        headers=admin,
        json={"name": unique("kb"), "access_scope": "public"},
    )
    assert knowledge_base.status_code == 201, knowledge_base.text
    valid_knowledge = await create_workflow(
        client, admin, definition=knowledge_definition([knowledge_base.json()["id"]])
    )
    assert (await validate(client, admin, valid_knowledge["id"]))["valid"] is True

    unknown_knowledge = await create_workflow(
        client, admin, definition=knowledge_definition([str(uuid4())])
    )
    result = await validate(client, admin, unknown_knowledge["id"])
    assert result["valid"] is False
    assert any("knowledge" in error.lower() for error in result["errors"])

    disabled_base = await client.post(
        "/api/v1/knowledge-bases",
        headers=admin,
        json={"name": unique("kb"), "access_scope": "public", "status": "disabled"},
    )
    assert disabled_base.status_code == 201, disabled_base.text
    disabled_knowledge = await create_workflow(
        client,
        admin,
        definition=knowledge_definition([disabled_base.json()["id"]]),
    )
    result = await validate(client, admin, disabled_knowledge["id"])
    assert result["valid"] is False
    assert any("disabled" in error for error in result["errors"])

    model_config_id = await create_model_config(client, admin)
    explicit_model = await create_workflow(
        client, admin, definition=llm_definition(model_config_id=model_config_id)
    )
    assert (await validate(client, admin, explicit_model["id"]))["valid"] is True

    default_model = await create_workflow(client, admin, definition=llm_definition())
    assert (await validate(client, admin, default_model["id"]))["valid"] is True

    unknown_model = await create_workflow(
        client, admin, definition=llm_definition(model_config_id=str(uuid4()))
    )
    result = await validate(client, admin, unknown_model["id"])
    assert result["valid"] is False
    assert any("Model config" in error for error in result["errors"])


async def test_validation_resolves_agent_bindings(
    client: AsyncClient, admin: dict[str, str]
) -> None:
    config_id = await create_model_config(client, admin)
    prompt_id = await create_prompt(client, admin)
    agent = await create_agent(
        client, admin, config_id=config_id, prompt_id=prompt_id
    )
    agent_workflow = await create_workflow(
        client, admin, definition=agent_definition(agent["id"])
    )
    result = await validate(client, admin, agent_workflow["id"])
    assert result["valid"] is True, result

    unknown_agent = await create_workflow(
        client, admin, definition=agent_definition(str(uuid4()))
    )
    result = await validate(client, admin, unknown_agent["id"])
    assert result["valid"] is False
    assert any("agent" in error.lower() for error in result["errors"])

    # A high-risk tool cannot be executed inside a workflow Agent node.
    high_risk_tool = await create_http_tool(
        client, admin, risk_level="high"
    )
    risky_agent = await create_agent(
        client,
        admin,
        config_id=config_id,
        prompt_id=prompt_id,
        tools=[high_risk_tool],
    )
    risky_workflow = await create_workflow(
        client, admin, definition=agent_definition(risky_agent["id"])
    )
    result = await validate(client, admin, risky_workflow["id"])
    assert result["valid"] is False
    assert any("approval" in error for error in result["errors"])
    assert any(high_risk_tool in error for error in result["errors"])

    # Workflow-mode agents are rejected to avoid workflow recursion.
    base_workflow = await create_workflow(client, admin)
    base_publish = await client.post(
        f"{WORKFLOWS}/{base_workflow['id']}/publish", headers=admin
    )
    assert base_publish.status_code == 200, base_publish.text
    workflow_agent = await create_agent(
        client,
        admin,
        config_id=config_id,
        prompt_id=prompt_id,
        mode="workflow",
        workflow_id=base_workflow["id"],
    )
    recursive = await create_workflow(
        client, admin, definition=agent_definition(workflow_agent["id"])
    )
    result = await validate(client, admin, recursive["id"])
    assert result["valid"] is False
    assert any("only plain agents" in error for error in result["errors"])

    publish = await client.post(
        f"{WORKFLOWS}/{recursive['id']}/publish", headers=admin
    )
    assert publish.status_code == 422, publish.text


async def test_disabled_and_draft_workflows_cannot_bind(
    client: AsyncClient, admin: dict[str, str], db: AsyncSession
) -> None:
    me = await client.get("/api/v1/auth/me", headers=admin)
    assert me.status_code == 200
    actor = await actor_for(db, me.json()["id"])

    workflow = await create_workflow(client, admin)
    workflow_id = workflow["id"]
    with pytest.raises(WorkflowNotPublishedError):
        await WorkflowService(db).validate_agent_binding(workflow_id, actor)

    published = await client.post(f"{WORKFLOWS}/{workflow_id}/publish", headers=admin)
    assert published.status_code == 200, published.text
    binding = await WorkflowService(db).validate_agent_binding(workflow_id, actor)
    assert binding.version == 1
    assert binding.workflow_name == workflow["name"]

    disabled = await client.post(f"{WORKFLOWS}/{workflow_id}/disable", headers=admin)
    assert disabled.status_code == 200
    with pytest.raises(WorkflowDisabledError):
        await WorkflowService(db).validate_agent_binding(workflow_id, actor)
    with pytest.raises(WorkflowDisabledError):
        await WorkflowService(db).load_published(workflow_id)

    config_id = await create_model_config(client, admin)
    prompt_id = await create_prompt(client, admin)
    agent = await create_agent(
        client,
        admin,
        config_id=config_id,
        prompt_id=prompt_id,
        mode="workflow",
        workflow_id=workflow_id,
        publish=False,
    )
    publish = await client.post(f"{AGENTS}/{agent['id']}/publish", headers=admin)
    assert publish.status_code == 422, publish.text
    assert "disabled" in publish.text


async def test_workflow_permissions_and_ownership(
    client: AsyncClient, admin: dict[str, str]
) -> None:
    _, viewer = await create_user(client, admin, role="viewer")
    developer_id, developer = await create_user(client, admin)
    other_id, other_developer = await create_user(client, admin)

    viewer_create = await client.post(
        WORKFLOWS,
        headers=viewer,
        json={"name": unique("viewer-workflow"), "definition": simple_definition()},
    )
    assert viewer_create.status_code == 403, viewer_create.text
    viewer_list = await client.get(WORKFLOWS, headers=viewer)
    assert viewer_list.status_code == 200

    workflow = await create_workflow(client, developer)
    workflow_id = workflow["id"]
    assert workflow["owner_id"] == developer_id

    viewer_read = await client.get(f"{WORKFLOWS}/{workflow_id}", headers=viewer)
    assert viewer_read.status_code == 200
    viewer_validate = await client.post(
        f"{WORKFLOWS}/{workflow_id}/validate", headers=viewer
    )
    assert viewer_validate.status_code == 200
    viewer_publish = await client.post(
        f"{WORKFLOWS}/{workflow_id}/publish", headers=viewer
    )
    assert viewer_publish.status_code == 403

    other_update = await client.put(
        f"{WORKFLOWS}/{workflow_id}",
        headers=other_developer,
        json={"description": "hijacked"},
    )
    assert other_update.status_code == 403, other_update.text
    other_publish = await client.post(
        f"{WORKFLOWS}/{workflow_id}/publish", headers=other_developer
    )
    assert other_publish.status_code == 403

    owner_publish = await client.post(
        f"{WORKFLOWS}/{workflow_id}/publish", headers=developer
    )
    assert owner_publish.status_code == 200, owner_publish.text
    assert owner_publish.json()["current_version"] == 1

    admin_publish = await client.post(
        f"{WORKFLOWS}/{workflow_id}/publish", headers=admin
    )
    assert admin_publish.status_code == 200, admin_publish.text
    assert admin_publish.json()["current_version"] == 2

    grant = await client.post(
        "/api/v1/permissions/grant",
        headers=admin,
        json={
            "resource_type": "workflow",
            "resource_id": workflow_id,
            "subject_type": "user",
            "subject_id": other_id,
            "actions": ["write", "publish"],
        },
    )
    assert grant.status_code == 201, grant.text
    granted_update = await client.put(
        f"{WORKFLOWS}/{workflow_id}",
        headers=other_developer,
        json={"description": "granted collaborator"},
    )
    assert granted_update.status_code == 200, granted_update.text
    granted_publish = await client.post(
        f"{WORKFLOWS}/{workflow_id}/publish", headers=other_developer
    )
    assert granted_publish.status_code == 200, granted_publish.text
    assert granted_publish.json()["current_version"] == 3

    delete = await client.delete(f"{WORKFLOWS}/{workflow_id}", headers=viewer)
    assert delete.status_code == 403

    # A fresh developer who is neither owner nor grantee cannot delete a draft.
    outsider = await create_workflow(client, admin)
    outsider_delete = await client.delete(
        f"{WORKFLOWS}/{outsider['id']}", headers=developer
    )
    assert outsider_delete.status_code == 403
    admin_delete = await client.delete(f"{WORKFLOWS}/{outsider['id']}", headers=admin)
    assert admin_delete.status_code == 204


async def test_workflow_service_rejects_invalid_actor(
    client: AsyncClient, admin: dict[str, str], db: AsyncSession
) -> None:
    workflow = await create_workflow(client, admin)
    me = await client.get("/api/v1/auth/me", headers=admin)
    actor = await actor_for(db, me.json()["id"])
    actor.permissions.discard("workflows:read")
    actor.permissions.discard("*")
    actor.is_superuser = False
    with pytest.raises(PermissionDeniedError):
        await WorkflowService(db).list_workflows(actor)
    with pytest.raises(PermissionDeniedError):
        await WorkflowService(db).validate(actor, workflow["id"])
