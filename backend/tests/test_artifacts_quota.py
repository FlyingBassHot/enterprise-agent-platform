from __future__ import annotations

import io
from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile

from app.core.exceptions import PayloadTooLargeError
from app.modules.execution.artifacts import ArtifactService
from app.modules.permission.service import PermissionService
from tests.conftest import admin_headers


async def test_artifact_quota_rejects_upload(
    client: AsyncClient, db: AsyncSession, monkeypatch
) -> None:
    headers = await admin_headers(client)
    me = await client.get("/api/v1/auth/me", headers=headers)
    actor = await PermissionService(db).load_context(me.json()["id"])

    settings = SimpleNamespace(
        max_upload_mb=50,
        user_storage_quota_mb=0.000001,
        storage_dir="./data/files",
    )
    monkeypatch.setattr(
        "app.modules.execution.artifacts.get_settings", lambda: settings
    )
    upload = UploadFile(
        file=io.BytesIO(b"x" * 16), filename="quota.txt", size=16
    )
    with pytest.raises(PayloadTooLargeError):
        await ArtifactService(db).save(actor, upload)
