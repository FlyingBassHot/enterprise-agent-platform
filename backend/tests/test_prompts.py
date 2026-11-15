from __future__ import annotations

from uuid import uuid4

from httpx import AsyncClient

from app.modules.prompt.models import Prompt, PromptVersion  # noqa: F401  (create_all)

PROMPTS = "/api/v1/prompts"


def unique_name(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:10]}"


async def create_prompt(client: AsyncClient, headers: dict[str, str], prefix: str = "prompt"):
    name = unique_name(prefix)
    response = await client.post(
        PROMPTS, headers=headers, json={"name": name, "description": "test prompt"}
    )
    assert response.status_code == 201, response.text
    return response.json()


def version_payload(**overrides):
    payload = {
        "system_prompt": "You are a {{role}} assistant for {{department}}.",
        "task_prompt": "Analyze {{target}} and report findings.",
        "tool_instructions": "Use tools when {{role}} needs data.",
        "variables": [
            {"name": "role", "required": True, "default": ""},
            {"name": "department", "required": False, "default": "general"},
            {"name": "target", "required": True, "default": ""},
        ],
    }
    payload.update(overrides)
    return payload


async def test_prompt_version_rendering_and_defaults(
    client: AsyncClient, admin: dict[str, str]
) -> None:
    prompt = await create_prompt(client, admin)
    prompt_id = prompt["id"]
    assert prompt["status"] == "draft"
    assert prompt["current_version"] == 0

    updated = await client.put(
        f"{PROMPTS}/{prompt_id}",
        headers=admin,
        json={"description": "updated while draft"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["description"] == "updated while draft"

    version = await client.post(
        f"{PROMPTS}/{prompt_id}/versions", headers=admin, json=version_payload()
    )
    assert version.status_code == 201, version.text
    version_body = version.json()
    assert version_body["version"] == 1
    assert version_body["prompt_id"] == prompt_id

    fetched = await client.get(f"{PROMPTS}/{prompt_id}", headers=admin)
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "published"
    assert fetched.json()["current_version"] == 1

    resolved = await client.post(
        f"{PROMPTS}/{prompt_id}/resolve",
        headers=admin,
        json={"variables": {"role": "audit", "target": "ledger"}},
    )
    assert resolved.status_code == 200, resolved.text
    resolved_body = resolved.json()
    assert resolved_body["version"] == 1
    assert resolved_body["version_id"] == version_body["id"]
    assert resolved_body["system_prompt"] == "You are a audit assistant for general."
    assert resolved_body["task_prompt"] == "Analyze ledger and report findings."
    assert resolved_body["tool_instructions"] == "Use tools when audit needs data."

    missing = await client.post(
        f"{PROMPTS}/{prompt_id}/resolve", headers=admin, json={"variables": {"role": "audit"}}
    )
    assert missing.status_code == 422, missing.text
    assert "target" in missing.json()["details"]["missing_variables"]

    unknown = await client.post(
        f"{PROMPTS}/{prompt_id}/resolve",
        headers=admin,
        json={"variables": {"role": "audit", "target": "x", "extra": "y"}},
    )
    assert unknown.status_code == 422, unknown.text

    published_update = await client.put(
        f"{PROMPTS}/{prompt_id}", headers=admin, json={"name": unique_name("renamed")}
    )
    assert published_update.status_code == 409, published_update.text


async def test_prompt_versions_are_immutable(client: AsyncClient, admin: dict[str, str]) -> None:
    prompt = await create_prompt(client, admin)
    prompt_id = prompt["id"]
    first = await client.post(
        f"{PROMPTS}/{prompt_id}/versions", headers=admin, json=version_payload()
    )
    assert first.status_code == 201, first.text
    first_id = first.json()["id"]

    second = await client.post(
        f"{PROMPTS}/{prompt_id}/versions",
        headers=admin,
        json=version_payload(system_prompt="V2 system prompt without variables."),
    )
    assert second.status_code == 201, second.text
    assert second.json()["version"] == 2

    versions = await client.get(f"{PROMPTS}/{prompt_id}/versions", headers=admin)
    assert versions.status_code == 200
    versions_body = versions.json()
    assert [item["version"] for item in versions_body] == [1, 2]
    assert versions_body[0]["system_prompt"].startswith("You are a {{role}}")

    current = await client.post(
        f"{PROMPTS}/{prompt_id}/resolve",
        headers=admin,
        json={"variables": {"role": "audit", "target": "ledger"}},
    )
    assert current.status_code == 200
    assert current.json()["version"] == 2
    assert current.json()["system_prompt"] == "V2 system prompt without variables."

    pinned = await client.post(
        f"{PROMPTS}/{prompt_id}/resolve",
        headers=admin,
        json={
            "version_id": first_id,
            "variables": {"role": "audit", "target": "ledger"},
        },
    )
    assert pinned.status_code == 200, pinned.text
    assert pinned.json()["system_prompt"] == "You are a audit assistant for general."


async def test_prompt_rejects_undeclared_placeholders(
    client: AsyncClient, admin: dict[str, str]
) -> None:
    prompt = await create_prompt(client, admin)
    response = await client.post(
        f"{PROMPTS}/{prompt['id']}/versions",
        headers=admin,
        json=version_payload(
            system_prompt="Hello {{ missing_placeholder }}",
            task_prompt="",
            tool_instructions="",
            variables=[],
        ),
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "validation_error"
    assert response.json()["details"]["unknown_placeholders"] == ["missing_placeholder"]


async def test_prompt_delete(client: AsyncClient, admin: dict[str, str]) -> None:
    prompt = await create_prompt(client, admin)
    prompt_id = prompt["id"]
    await client.post(f"{PROMPTS}/{prompt_id}/versions", headers=admin, json=version_payload())
    deleted = await client.delete(f"{PROMPTS}/{prompt_id}", headers=admin)
    assert deleted.status_code == 204, deleted.text
    missing = await client.get(f"{PROMPTS}/{prompt_id}", headers=admin)
    assert missing.status_code == 404
    versions = await client.get(f"{PROMPTS}/{prompt_id}/versions", headers=admin)
    assert versions.status_code == 404
