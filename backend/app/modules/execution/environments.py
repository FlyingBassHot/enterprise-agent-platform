"""Artifact workspace: the environment a run works in.

The workspace is deliberately narrow: it declares which uploads are visible to
this run and reads them through the existing artifact ACL + size cap. It is not
a filesystem and it grants no new capability — building that without a sandbox
would be a lie.

Scope rule: the workspace is (a) artifacts of the execution's conversation and
(b) the artifacts explicitly attached to the execution. Reading anything else
through the workspace is rejected, even if the user owns it.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.contracts import (
    EnvironmentDescriptor,
    EnvironmentResource,
    UserContext,
)
from app.core.exceptions import NotFoundError, PermissionDeniedError, ValidationError
from app.infrastructure.database.session import get_session_factory
from app.modules.execution.artifacts import read_artifact_for_tool
from app.modules.execution.models import Artifact
from app.modules.execution.repository import ExecutionRepository

KIND = "artifact_workspace"
MAX_DECLARED_RESOURCES = 200


class ArtifactWorkspace:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = ExecutionRepository(db)

    async def open(self, *, execution_id: str, user: UserContext) -> EnvironmentDescriptor:
        execution = await self.repo.get_execution(execution_id)
        if execution is None:
            raise NotFoundError(f"Execution {execution_id} not found")
        if not user.is_superuser and execution.user_id != user.user_id:
            raise PermissionDeniedError("No access to this execution's workspace")
        rows = await self._resources(execution_id, execution.conversation_id, execution.input_payload or {})
        return EnvironmentDescriptor(
            kind=KIND,
            environment_id=execution_id,
            label=f"conversation {execution.conversation_id or 'detached'}",
            resources=[_to_resource(row) for row in rows[:MAX_DECLARED_RESOURCES]],
            constraints={
                "read_only": True,
                "max_resource_bytes": int(get_settings().environment_max_resource_bytes),
                "truncated": len(rows) > MAX_DECLARED_RESOURCES,
            },
        )

    async def close(self, environment_id: str) -> None:
        """No leased resources yet; future sandboxes release handles here."""

    async def read(
        self,
        *,
        execution_id: str,
        user: UserContext,
        resource_id: str,
        max_bytes: int | None = None,
    ) -> tuple[EnvironmentResource, bytes]:
        execution = await self.repo.get_execution(execution_id)
        if execution is None:
            raise NotFoundError(f"Execution {execution_id} not found")
        if not user.is_superuser and execution.user_id != user.user_id:
            raise PermissionDeniedError("No access to this execution's workspace")
        rows = await self._resources(execution_id, execution.conversation_id, execution.input_payload or {})
        artifact = next((row for row in rows if row.id == resource_id), None)
        if artifact is None:
            raise NotFoundError(
                f"Artifact {resource_id} is not part of this execution's workspace"
            )
        cap = int(get_settings().environment_max_resource_bytes)
        if max_bytes is not None:
            cap = min(cap, max_bytes)
        if artifact.size > cap:
            raise ValidationError(
                f"Artifact {artifact.filename} exceeds the workspace read limit "
                f"({artifact.size} > {cap} bytes)"
            )
        info, data = await read_artifact_for_tool(resource_id, user)
        resource = EnvironmentResource(
            id=info.id,
            name=info.filename,
            kind="artifact",
            content_type=info.content_type or "",
            size=info.size,
            access="read",
        )
        return resource, data

    async def _resources(
        self,
        execution_id: str,
        conversation_id: str | None,
        payload: dict[str, Any],
    ) -> list[Artifact]:
        rows: dict[str, Artifact] = {}
        if conversation_id:
            for row in await self.repo.list_conversation_artifacts(conversation_id):
                rows[row.id] = row
        attached = payload.get("artifact_ids") or []
        if attached:
            for row in await self.repo.list_artifacts(list(attached)):
                rows[row.id] = row
        return list(rows.values())


def _to_resource(row: Artifact) -> EnvironmentResource:
    return EnvironmentResource(
        id=row.id,
        name=row.filename,
        kind="artifact",
        content_type=row.content_type or "",
        size=row.size,
        access="read",
    )


async def open_workspace_for_tool(
    execution_id: str, user: UserContext
) -> EnvironmentDescriptor:
    """Native tools read through the same boundary as the runtime."""

    async with get_session_factory()() as db:
        return await ArtifactWorkspace(db).open(execution_id=execution_id, user=user)


async def read_workspace_resource(
    execution_id: str,
    resource_id: str,
    user: UserContext,
    *,
    max_bytes: int | None = None,
) -> tuple[EnvironmentResource, bytes]:
    async with get_session_factory()() as db:
        return await ArtifactWorkspace(db).read(
            execution_id=execution_id,
            user=user,
            resource_id=resource_id,
            max_bytes=max_bytes,
        )


__all__ = [
    "ArtifactWorkspace",
    "KIND",
    "open_workspace_for_tool",
    "read_workspace_resource",
]
