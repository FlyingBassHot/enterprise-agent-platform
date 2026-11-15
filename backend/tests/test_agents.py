from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError
from app.modules.agent.models import Agent, AgentVersion  # noqa: F401  (create_all)
from app.modules.agent.service import AgentService
from app.modules.prompt.models import Prompt, PromptVersion  # noqa: F401  (create_all)

AGENTS = "/api/v1/agents"
PROMPTS = "/api/v1/prompts"


def unique_name(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:10]}"


async def login(client: AsyncClient, username: str, password: str) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def create_user(
    client: AsyncClient,
    admin: dict[str, str],
    *,
    role: str = "developer",
    password: str = "user-pass-123",
) -> tuple[str, dict[str, str], str]:
    username = unique_name(f"user-{role}")
    created = await client.post(
        "/api/v1/users",
        headers=admin,
        json={
            "username": username,
            "password": password,
            "display_name": username,
            "role_names": [role],
        },
    )
    assert created.status_code == 201, created.text
    headers = await login(client, username, password)
    return username, headers, created.json()["id"]


async def create_model_config(
    client: AsyncClient, admin: dict[str, str], *, supports_tools: bool = True
) -> str:
    provider = await client.post(
        "/api/v1/model-providers",
        headers=admin,
        json={"name": unique_name("provider"), "provider_type": "mock"},
    )
    assert provider.status_code == 201, provider.text
    config = await client.post(
        "/api/v1/model-configs",
        headers=admin,
        json={
            "name": unique_name("config"),
            "provider_id": provider.json()["id"],
            "model_name": "mock-agent",
            "capabilities": {"supports_tools": supports_tools},
        },
    )
    assert config.status_code == 201, config.text
    return config.json()["id"]


async def create_prompt(
    client: AsyncClient,
    admin: dict[str, str],
    *,
    system_prompt: str = "You are a {{department}} analyst.",
    variables: list[dict] | None = None,
) -> dict:
    prompt = await client.post(
        PROMPTS, headers=admin, json={"name": unique_name("prompt")}
    )
    assert prompt.status_code == 201, prompt.text
    version = await client.post(
        f"{PROMPTS}/{prompt.json()['id']}/versions",
        headers=admin,
        json={
            "system_prompt": system_prompt,
            "task_prompt": "Analyze the request.",
            "variables": variables
            if variables is not None
            else [{"name": "department", "required": True, "default": ""}],
        },
    )
    assert version.status_code == 201, version.text
    return {"prompt": prompt.json(), "version": version.json()}


def agent_config(
    *,
    config_id: str,
    prompt_id: str,
    prompt_variables: dict[str, str] | None = None,
    tools: list[str] | None = None,
    temperature: float = 0.1,
    mode: str = "agent",
    approval_risk_levels: list[str] | None = None,
) -> dict:
    return {
        "model": {
            "config_id": config_id,
            "temperature": temperature,
            "max_tokens": 1024,
            "top_p": 0.9,
        },
        "prompt": {
            "prompt_id": prompt_id,
            "variables": {"department": "finance"}
            if prompt_variables is None
            else prompt_variables,
        },
        "tools": tools or [],
        "execution": {
            "mode": mode,
            "max_iterations": 6,
            "timeout_seconds": 300,
            "approval_risk_levels": approval_risk_levels or ["high", "critical"],
            "streaming": True,
        },
        "permissions": {
            "required_permissions": ["agents:read"],
            "allowed_roles": ["developer"],
        },
        "metadata": {"team": "audit"},
    }


async def create_agent(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    config_id: str,
    prompt_id: str,
    **config_overrides,
) -> dict:
    response = await client.post(
        AGENTS,
        headers=headers,
        json={
            "name": unique_name("agent"),
            "description": "agent under test",
            "config": agent_config(
                config_id=config_id, prompt_id=prompt_id, **config_overrides
            ),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_agent_publish_and_version_immutability(
    client: AsyncClient, admin: dict[str, str], db: AsyncSession
) -> None:
    config_id = await create_model_config(client, admin)
    prompt_bundle = await create_prompt(client, admin)
    agent = await create_agent(
        client, admin, config_id=config_id, prompt_id=prompt_bundle["prompt"]["id"]
    )
    agent_id = agent["id"]
    assert agent["status"] == "draft"
    assert agent["owner_id"]

    validation = await client.post(f"{AGENTS}/{agent_id}/validate", headers=admin)
    assert validation.status_code == 200, validation.text
    assert validation.json()["valid"] is True, validation.json()

    published = await client.post(f"{AGENTS}/{agent_id}/publish", headers=admin)
    assert published.status_code == 200, published.text
    published_body = published.json()
    assert published_body["status"] == "published"
    assert published_body["current_version"] == 1
    assert published_body["current_version_id"]
    assert published_body["published_at"] is not None

    versions = await client.get(f"{AGENTS}/{agent_id}/versions", headers=admin)
    assert versions.status_code == 200
    first_version = versions.json()[0]
    assert first_version["version"] == 1
    assert first_version["snapshot"]["system_prompt"] == "You are a finance analyst."
    assert first_version["snapshot"]["agent_id"] == agent_id
    assert first_version["snapshot"]["mode"] == "agent"
    assert first_version["snapshot"]["prompt_variables"] == {"department": "finance"}
    assert first_version["snapshot"]["max_iterations"] == 6
    assert first_version["snapshot"]["model"]["config_id"] == config_id
    assert first_version["snapshot"]["model"]["temperature"] == 0.1
    assert published_body["current_version_id"] == first_version["id"]

    snapshot = await AgentService(db).load_published(agent_id)
    assert snapshot.version == 1
    assert snapshot.system_prompt == "You are a finance analyst."

    updated = await client.put(
        f"{AGENTS}/{agent_id}",
        headers=admin,
        json={
            "config": agent_config(
                config_id=config_id,
                prompt_id=prompt_bundle["prompt"]["id"],
                prompt_variables={"department": "risk"},
                temperature=0.7,
            )
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["current_version"] == 1
    assert updated.json()["draft_config"]["model"]["temperature"] == 0.7

    versions_after_edit = await client.get(f"{AGENTS}/{agent_id}/versions", headers=admin)
    assert versions_after_edit.json()[0] == first_version

    second = await client.post(f"{AGENTS}/{agent_id}/publish", headers=admin)
    assert second.status_code == 200, second.text
    assert second.json()["current_version"] == 2

    versions = (await client.get(f"{AGENTS}/{agent_id}/versions", headers=admin)).json()
    assert [item["version"] for item in versions] == [1, 2]
    assert versions[0]["snapshot"]["system_prompt"] == "You are a finance analyst."
    pinned = await AgentService(db).load_version(agent_id, first_version["id"])
    assert pinned.version == 1
    assert pinned.model.temperature == 0.1
    assert (await AgentService(db).load_published(agent_id)).version == 2
    assert versions[1]["snapshot"]["system_prompt"] == "You are a risk analyst."
    assert versions[1]["config"]["model"]["temperature"] == 0.7

    disabled = await client.post(f"{AGENTS}/{agent_id}/disable", headers=admin)
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["status"] == "disabled"
    with pytest.raises(ConflictError):
        await AgentService(db).load_published(agent_id)


async def test_agent_validation_rejects_invalid_configuration(
    client: AsyncClient, admin: dict[str, str]
) -> None:
    config_id = await create_model_config(client, admin)
    prompt_bundle = await create_prompt(client, admin)
    prompt_id = prompt_bundle["prompt"]["id"]

    missing_variable = await create_agent(
        client,
        admin,
        config_id=config_id,
        prompt_id=prompt_id,
        prompt_variables={},
    )
    validation = await client.post(
        f"{AGENTS}/{missing_variable['id']}/validate", headers=admin
    )
    assert validation.status_code == 200
    assert validation.json()["valid"] is False
    assert any("Missing required prompt variables" in e for e in validation.json()["errors"])
    publish = await client.post(f"{AGENTS}/{missing_variable['id']}/publish", headers=admin)
    assert publish.status_code == 422, publish.text

    missing_tool = await create_agent(
        client,
        admin,
        config_id=config_id,
        prompt_id=prompt_id,
        tools=["tool-that-does-not-exist"],
    )
    validation = await client.post(f"{AGENTS}/{missing_tool['id']}/validate", headers=admin)
    assert validation.status_code == 200, validation.text
    assert validation.json()["valid"] is False
    assert any("tool-that-does-not-exist" in e for e in validation.json()["errors"])
    publish = await client.post(f"{AGENTS}/{missing_tool['id']}/publish", headers=admin)
    assert publish.status_code == 422, publish.text

    workflow_mode = await client.post(
        AGENTS,
        headers=admin,
        json={
            "name": unique_name("workflow-agent"),
            "config": agent_config(
                config_id=config_id, prompt_id=prompt_id, mode="workflow"
            ),
        },
    )
    assert workflow_mode.status_code == 201, workflow_mode.text
    workflow_validation = await client.post(
        f"{AGENTS}/{workflow_mode.json()['id']}/validate", headers=admin
    )
    assert workflow_validation.status_code == 200
    assert workflow_validation.json()["valid"] is False
    assert any("workflow_id" in e for e in workflow_validation.json()["errors"])
    workflow_publish = await client.post(
        f"{AGENTS}/{workflow_mode.json()['id']}/publish", headers=admin
    )
    assert workflow_publish.status_code == 422, workflow_publish.text

    bad_risk = await client.post(
        AGENTS,
        headers=admin,
        json={
            "name": unique_name("risk-agent"),
            "config": agent_config(
                config_id=config_id,
                prompt_id=prompt_id,
                approval_risk_levels=["extreme"],
            ),
        },
    )
    assert bad_risk.status_code == 422, bad_risk.text

    unknown_model = await create_agent(
        client, admin, config_id="missing-model-config", prompt_id=prompt_id
    )
    validation = await client.post(f"{AGENTS}/{unknown_model['id']}/validate", headers=admin)
    assert validation.json()["valid"] is False
    assert any("Model configuration" in e for e in validation.json()["errors"])


async def test_agent_permissions_and_ownership(
    client: AsyncClient, admin: dict[str, str]
) -> None:
    config_id = await create_model_config(client, admin)
    prompt_bundle = await create_prompt(client, admin)
    prompt_id = prompt_bundle["prompt"]["id"]

    _, viewer, _ = await create_user(client, admin, role="viewer")
    viewer_create = await client.post(
        AGENTS,
        headers=viewer,
        json={
            "name": unique_name("viewer-agent"),
            "config": agent_config(config_id=config_id, prompt_id=prompt_id),
        },
    )
    assert viewer_create.status_code == 403, viewer_create.text

    _, developer_one, _ = await create_user(client, admin)
    _, developer_two, _ = await create_user(client, admin)
    agent = await create_agent(
        client, developer_one, config_id=config_id, prompt_id=prompt_id
    )

    other_publish = await client.post(f"{AGENTS}/{agent['id']}/publish", headers=developer_two)
    assert other_publish.status_code == 403, other_publish.text
    other_update = await client.put(
        f"{AGENTS}/{agent['id']}",
        headers=developer_two,
        json={"description": "hijacked"},
    )
    assert other_update.status_code == 403, other_update.text

    owner_publish = await client.post(f"{AGENTS}/{agent['id']}/publish", headers=developer_one)
    assert owner_publish.status_code == 200, owner_publish.text

    admin_publish = await client.post(f"{AGENTS}/{agent['id']}/publish", headers=admin)
    assert admin_publish.status_code == 200, admin_publish.text
    assert admin_publish.json()["current_version"] == 2

    developer_two_id = (
        await client.get("/api/v1/auth/me", headers=developer_two)
    ).json()["id"]
    grant = await client.post(
        "/api/v1/permissions/grant",
        headers=admin,
        json={
            "resource_type": "agent",
            "resource_id": agent["id"],
            "subject_type": "user",
            "subject_id": developer_two_id,
            "actions": ["write", "publish"],
        },
    )
    assert grant.status_code == 201, grant.text
    granted_update = await client.put(
        f"{AGENTS}/{agent['id']}",
        headers=developer_two,
        json={"description": "granted collaborator"},
    )
    assert granted_update.status_code == 200, granted_update.text
    granted_publish = await client.post(f"{AGENTS}/{agent['id']}/publish", headers=developer_two)
    assert granted_publish.status_code == 200, granted_publish.text
    assert granted_publish.json()["current_version"] == 3

    viewer_read = await client.get(f"{AGENTS}/{agent['id']}", headers=viewer)
    assert viewer_read.status_code == 200
    viewer_publish = await client.post(f"{AGENTS}/{agent['id']}/publish", headers=viewer)
    assert viewer_publish.status_code == 403

    listing = await client.get(AGENTS, headers=developer_one)
    assert listing.status_code == 200
    assert any(item["id"] == agent["id"] for item in listing.json())
