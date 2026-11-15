from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.permission.schemas import AccessPolicyCreate
from app.modules.permission.service import PermissionService
from tests.conftest import admin_headers


async def test_abac_allow_deny_and_superuser_bypass(
    client: AsyncClient, db: AsyncSession
) -> None:
    headers = await admin_headers(client)
    me = await client.get("/api/v1/auth/me", headers=headers)
    admin = await PermissionService(db).load_context(me.json()["id"])
    service = PermissionService(db)

    await service.create_policy(
        admin,
        AccessPolicyCreate(
            name="viewer-abac-document-read",
            resource_type="abac_document",
            action="read",
            subject_roles=["viewer"],
            priority=0,
        ),
    )
    context = admin.model_copy(
        update={"is_superuser": False, "roles": ["viewer"], "permissions": set()}
    )
    assert await service.can_access_resource(
        context, resource_type="abac_document", resource_id="doc-1", action="read"
    )

    await service.create_policy(
        admin,
        AccessPolicyCreate(
            name="block-viewer-abac-document",
            resource_type="abac_document",
            action="read",
            subject_roles=["viewer"],
            effect="deny",
            priority=10,
        ),
    )
    assert not await service.can_access_resource(
        context,
        resource_type="abac_document",
        resource_id="doc-1",
        action="read",
        owner_id=context.user_id,
    )
    assert await service.can_access_resource(
        admin, resource_type="abac_document", resource_id="doc-1", action="read"
    )


async def test_policy_api_permissions(
    client: AsyncClient, admin: dict[str, str]
) -> None:
    created = await client.post(
        "/api/v1/access-policies",
        headers=admin,
        json={
            "name": "api-policy",
            "resource_type": "abac_workflow",
            "action": "read",
            "subject_roles": ["operator"],
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["effect"] == "allow"

    await client.post(
        "/api/v1/users",
        headers=admin,
        json={
            "username": "policy-developer",
            "password": "developer-pass-123",
            "role_names": ["developer"],
        },
    )
    login = await client.post(
        "/api/v1/auth/login",
        json={"username": "policy-developer", "password": "developer-pass-123"},
    )
    forbidden = await client.get(
        "/api/v1/access-policies",
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
    )
    assert forbidden.status_code == 403
