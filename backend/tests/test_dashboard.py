from __future__ import annotations

from httpx import AsyncClient


async def test_dashboard_stats_shape_and_permissions(
    client: AsyncClient, admin: dict[str, str]
) -> None:
    response = await client.get("/api/v1/dashboard/stats", headers=admin)
    assert response.status_code == 200, response.text
    body = response.json()
    for field in (
        "agents_total",
        "agents_published",
        "tools_total",
        "tools_enabled",
        "executions_total",
        "success_rate",
        "tokens_total",
        "status_breakdown",
        "recent_executions",
    ):
        assert field in body
    assert body["tools_total"] >= 2  # audit plugin tools synced on startup
    assert 0.0 <= body["success_rate"] <= 100.0
    assert isinstance(body["recent_executions"], list)

    await client.post(
        "/api/v1/users",
        headers=admin,
        json={
            "username": "dashboard-viewer",
            "password": "viewer-pass-123",
            "role_names": ["viewer"],
        },
    )
    login = await client.post(
        "/api/v1/auth/login",
        json={"username": "dashboard-viewer", "password": "viewer-pass-123"},
    )
    viewer_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    allowed = await client.get("/api/v1/dashboard/stats", headers=viewer_headers)
    assert allowed.status_code == 200
    assert allowed.json()["executions_total"] == 0
    assert allowed.json()["recent_executions"] == []
