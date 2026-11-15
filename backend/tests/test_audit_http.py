from __future__ import annotations

from httpx import AsyncClient


async def test_audit_log_records_login_and_mutations(
    client: AsyncClient, admin: dict[str, str]
) -> None:
    failed = await client.post(
        "/api/v1/auth/login",
        json={"username": "audit-ghost", "password": "wrong-password"},
    )
    assert failed.status_code == 401

    denied = await client.get(
        "/api/v1/audit-logs", params={"action": "auth.login", "result": "denied"}, headers=admin
    )
    assert denied.status_code == 200, denied.text
    assert any(row["actor_username"] == "audit-ghost" for row in denied.json())

    created = await client.post(
        "/api/v1/users",
        headers=admin,
        json={
            "username": "audit-operator",
            "password": "operator-pass-123",
            "role_names": ["operator"],
        },
    )
    assert created.status_code == 201, created.text

    logs = await client.get(
        "/api/v1/audit-logs",
        params={"action": "POST /api/v1/users"},
        headers=admin,
    )
    assert logs.status_code == 200, logs.text
    actions = [row["action"] for row in logs.json()]
    assert "POST /api/v1/users" in actions
    assert any(row["actor_id"] and row["result"] == "success" for row in logs.json())

    login = await client.post(
        "/api/v1/auth/login",
        json={"username": "audit-operator", "password": "operator-pass-123"},
    )
    assert login.status_code == 200
    forbidden = await client.get(
        "/api/v1/audit-logs",
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
    )
    assert forbidden.status_code == 403
