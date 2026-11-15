from __future__ import annotations

from httpx import AsyncClient


async def test_bootstrap_login_and_me(client: AsyncClient) -> None:
    from tests.conftest import admin_headers

    headers = await admin_headers(client)
    response = await client.get("/api/v1/auth/me", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["username"] == "admin"
    assert body["is_superuser"] is True
    assert "admin" in body["roles"]


async def test_user_management_and_rbac(client: AsyncClient, admin: dict[str, str]) -> None:
    response = await client.post(
        "/api/v1/users",
        headers=admin,
        json={
            "username": "viewer1",
            "password": "viewer-pass-123",
            "display_name": "Viewer",
            "role_names": ["viewer"],
        },
    )
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["roles"] == ["viewer"]

    login = await client.post(
        "/api/v1/auth/login",
        json={"username": "viewer1", "password": "viewer-pass-123"},
    )
    assert login.status_code == 200
    viewer_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    forbidden = await client.post(
        "/api/v1/users",
        headers=viewer_headers,
        json={
            "username": "viewer2",
            "password": "viewer-pass-123",
            "role_names": ["viewer"],
        },
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "permission_denied"

    allowed = await client.get("/api/v1/model-configs", headers=viewer_headers)
    assert allowed.status_code == 200


async def test_wrong_password(client: AsyncClient, admin: dict[str, str]) -> None:
    response = await client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "wrong-password"}
    )
    assert response.status_code == 401
