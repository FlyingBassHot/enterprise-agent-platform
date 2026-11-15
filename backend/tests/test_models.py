from __future__ import annotations

from httpx import AsyncClient


async def test_mock_provider_and_config_lifecycle(
    client: AsyncClient, admin: dict[str, str]
) -> None:
    provider = await client.post(
        "/api/v1/model-providers",
        headers=admin,
        json={
            "name": "mock-local",
            "provider_type": "mock",
            "description": "offline demo provider",
        },
    )
    assert provider.status_code == 201, provider.text
    provider_body = provider.json()
    assert provider_body["provider_type"] == "mock"

    config = await client.post(
        "/api/v1/model-configs",
        headers=admin,
        json={
            "name": "mock-fast",
            "display_name": "Mock Fast",
            "provider_id": provider_body["id"],
            "model_name": "mock-agent",
            "is_default": True,
            "capabilities": {"supports_tools": True},
        },
    )
    assert config.status_code == 201, config.text
    config_body = config.json()
    assert config_body["provider_name"] == "mock-local"
    assert config_body["is_default"] is True

    health = await client.post(
        f"/api/v1/model-configs/{config_body['id']}/health", headers=admin
    )
    assert health.status_code == 200
    assert health.json()["ok"] is True

    configs = await client.get("/api/v1/model-configs", headers=admin)
    assert configs.status_code == 200
    assert any(item["id"] == config_body["id"] for item in configs.json())


async def test_provider_url_change_clears_stored_key(
    client: AsyncClient, admin: dict[str, str]
) -> None:
    provider = await client.post(
        "/api/v1/model-providers",
        headers=admin,
        json={
            "name": "secret-provider",
            "provider_type": "openai_compatible",
            "base_url": "https://llm.example.test/v1",
            "api_key": "sk-test-secret-value",
        },
    )
    assert provider.status_code == 201, provider.text
    provider_body = provider.json()
    assert provider_body["has_api_key"] is True

    updated = await client.patch(
        f"/api/v1/model-providers/{provider_body['id']}",
        headers=admin,
        json={"base_url": "https://attacker.example/v1"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["has_api_key"] is False


async def test_developer_cannot_change_model_providers(
    client: AsyncClient, admin: dict[str, str]
) -> None:
    await client.post(
        "/api/v1/users",
        headers=admin,
        json={
            "username": "model-developer",
            "password": "developer-pass-123",
            "role_names": ["developer"],
        },
    )
    login = await client.post(
        "/api/v1/auth/login",
        json={"username": "model-developer", "password": "developer-pass-123"},
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    providers = await client.get("/api/v1/model-providers", headers=headers)
    assert providers.status_code == 200
    forbidden = await client.post(
        "/api/v1/model-providers",
        headers=headers,
        json={
            "name": "developer-provider",
            "provider_type": "mock",
        },
    )
    assert forbidden.status_code == 403
