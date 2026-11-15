"""Artifact upload/read service.

Uploads are validated and stored through the local storage backend; access is
owner/superuser/resource-grant based so tools can fetch files on behalf of the
executing user.
"""

from __future__ import annotations

import mimetypes
from datetime import datetime
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.contracts import ArtifactInfo, UserContext
from app.core.exceptions import (
    NotFoundError,
    PayloadTooLargeError,
    PermissionDeniedError,
    ValidationError,
)
from app.core.ids import new_id
from app.infrastructure.database.session import get_session_factory
from app.infrastructure.storage.local import LocalFileStorage, get_storage
from app.modules.execution.models import Artifact
from app.modules.execution.repository import ExecutionRepository
from app.modules.execution.schemas import ArtifactOut
from app.modules.permission.service import PermissionService

ALLOWED_EXTENSIONS = {".xlsx", ".xls", ".csv", ".pdf", ".docx", ".txt", ".md"}
_CHUNK_SIZE = 1024 * 1024


class ArtifactService:
    def __init__(self, db: AsyncSession, storage: LocalFileStorage | None = None) -> None:
        self.db = db
        self.repo = ExecutionRepository(db)
        self.storage = storage or get_storage()
        self.permissions = PermissionService(db)

    async def save(
        self,
        actor: UserContext,
        upload: UploadFile,
        *,
        conversation_id: str | None = None,
    ) -> ArtifactOut:
        filename = (upload.filename or "").strip()
        if not filename:
            raise ValidationError("A file name is required")
        extension = Path(filename).suffix.lower()
        if extension not in ALLOWED_EXTENSIONS:
            raise ValidationError(
                f"Unsupported file type '{extension or 'unknown'}'; "
                f"allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
            )
        settings = get_settings()
        max_bytes = settings.max_upload_mb * 1024 * 1024
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await upload.read(_CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValidationError(f"File exceeds the {settings.max_upload_mb} MB limit")
            chunks.append(chunk)
        current_usage = await self.repo.sum_artifact_size(actor.user_id)
        quota_bytes = max(0, settings.user_storage_quota_mb) * 1024 * 1024
        if quota_bytes and current_usage + total > quota_bytes:
            raise PayloadTooLargeError(
                f"Artifact storage quota exceeded ({settings.user_storage_quota_mb} MB)"
            )
        data = b"".join(chunks)

        if conversation_id:
            conversation = await self.repo.get_conversation(conversation_id)
            if conversation is None:
                raise NotFoundError("Conversation not found")
            if not actor.is_superuser and conversation.user_id != actor.user_id:
                raise PermissionDeniedError("No access to this conversation")

        artifact_id = new_id()
        storage_key = f"artifacts/{artifact_id}{extension}"
        content_type = (
            mimetypes.guess_type(filename)[0] or "application/octet-stream"
        )
        stored = await self.storage.put(storage_key, data, content_type)
        artifact = Artifact(
            id=artifact_id,
            owner_id=actor.user_id,
            filename=filename,
            content_type=content_type,
            size=stored.size,
            storage_key=storage_key,
            conversation_id=conversation_id,
        )
        await self.repo.create_artifact(artifact)
        await self.db.commit()
        return ArtifactOut.model_validate(artifact)

    async def _load(self, actor: UserContext, artifact_id: str) -> Artifact:
        artifact = await self.repo.get_artifact(artifact_id)
        if artifact is None:
            raise NotFoundError(f"Artifact {artifact_id} not found")
        await self._require_read(actor, artifact)
        return artifact

    async def _require_read(self, actor: UserContext, artifact: Artifact) -> None:
        if actor.is_superuser or artifact.owner_id == actor.user_id:
            return
        try:
            await self.permissions.require_resource_access(
                actor,
                resource_type="artifact",
                resource_id=artifact.id,
                action="read",
                owner_id=artifact.owner_id,
            )
        except PermissionDeniedError:
            raise PermissionDeniedError(
                f"No read access to artifact {artifact.id}"
            ) from None

    async def get_meta(self, actor: UserContext, artifact_id: str) -> ArtifactOut:
        artifact = await self._load(actor, artifact_id)
        return ArtifactOut.model_validate(artifact)

    async def read(self, actor: UserContext, artifact_id: str) -> tuple[ArtifactOut, bytes]:
        artifact = await self._load(actor, artifact_id)
        data = await self.storage.get(artifact.storage_key)
        return ArtifactOut.model_validate(artifact), data

    async def file_path(self, actor: UserContext, artifact_id: str) -> tuple[ArtifactOut, Path]:
        artifact = await self._load(actor, artifact_id)
        root = self.storage.root
        path = (root / artifact.storage_key).resolve()
        if not str(path).startswith(str(root)) or not path.is_file():
            raise NotFoundError(f"Artifact {artifact_id} content is missing")
        return ArtifactOut.model_validate(artifact), path

    async def info_for_tool(self, artifact_id: str, actor: UserContext) -> ArtifactInfo:
        artifact = await self._load(actor, artifact_id)
        return _to_info(artifact)


def _to_info(artifact: Artifact) -> ArtifactInfo:
    created_at: str | None = None
    value: datetime | None = artifact.created_at
    if value is not None:
        created_at = value.isoformat()
    return ArtifactInfo(
        id=artifact.id,
        filename=artifact.filename,
        content_type=artifact.content_type,
        size=artifact.size,
        storage_key=artifact.storage_key,
        owner_id=artifact.owner_id,
        created_at=created_at,
    )


async def read_artifact_for_tool(
    artifact_id: str, user: UserContext
) -> tuple[ArtifactInfo, bytes]:
    """Native tools use this helper; it opens its own session."""
    async with get_session_factory()() as db:
        service = ArtifactService(db)
        artifact = await service._load(user, artifact_id)
        data = await service.storage.get(artifact.storage_key)
        return _to_info(artifact), data


__all__ = ["ALLOWED_EXTENSIONS", "ArtifactService", "read_artifact_for_tool"]
