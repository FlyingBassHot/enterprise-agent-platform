"""Agent-scoped workspace: usable agents, conversations, runs, tasks, files."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.contracts import UserContext
from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from app.core.ids import new_id
from app.modules.agent.models import Agent
from app.modules.agent.repository import AgentRepository
from app.modules.agent.service import AgentService
from app.modules.audit_log.service import AuditService
from app.modules.execution.repository import ExecutionRepository
from app.modules.execution.schemas import ConversationOut, ExecutionOut, MessageOut, TaskOut
from app.modules.permission.service import PermissionService
from app.modules.workspace.models import AgentFileRoot, FileRoot
from app.modules.workspace.repository import WorkspaceRepository
from app.modules.workspace.schemas import (
    FileRootAccessOut,
    FileRootCreate,
    FileRootEntryOut,
    FileRootGrantCreate,
    FileRootGrantOut,
    FileRootOut,
    UsableAgentOut,
)

_CHUNK_SIZE = 1024 * 1024


class WorkspaceService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = WorkspaceRepository(db)
        self.executions = ExecutionRepository(db)
        self.permissions = PermissionService(db)
        self.agents = AgentService(db)

    # ------------------------------------------------------------- agent access

    async def list_usable_agents(self, actor: UserContext) -> list[UsableAgentOut]:
        return [
            UsableAgentOut(
                id=agent.id,
                name=agent.name,
                description=agent.description,
                status=agent.status,
                current_version=snapshot.version,
                published_at=agent.published_at,
            )
            for agent, snapshot in await self.agents.list_usable(actor)
        ]

    async def _usable_agent(self, agent_id: str, actor: UserContext) -> Agent:
        agent, _ = await self.agents.load_usable_agent(agent_id, actor)
        return agent

    # -------------------------------------------------------------- conversations

    async def list_conversations(
        self, actor: UserContext, agent_id: str, *, limit: int = 100
    ) -> list[ConversationOut]:
        agent = await self._usable_agent(agent_id, actor)
        rows = await self.executions.list_conversations(
            user_id=None if actor.is_superuser else actor.user_id,
            agent_id=agent.id,
            limit=limit,
        )
        return [ConversationOut.model_validate(row) for row in rows]

    async def create_conversation(
        self,
        actor: UserContext,
        agent_id: str,
        *,
        title: str = "",
        root_id: str | None = None,
    ) -> ConversationOut:
        agent = await self._usable_agent(agent_id, actor)
        if root_id:
            root = await self._root_or_404(root_id)
            await self._require_agent_root(actor, agent, root, "read")
        conversation = await self.executions.create_conversation(
            agent_id=agent.id,
            user_id=actor.user_id,
            root_id=root_id or None,
            title=title.strip() or "新对话",
        )
        await self.db.commit()
        return ConversationOut.model_validate(conversation)

    async def require_agent_root(
        self, actor: UserContext, agent_id: str, root_id: str
    ) -> FileRoot:
        """Public guard reused by execution creation and file tools."""

        agent = await self._usable_agent(agent_id, actor)
        root = await self._root_or_404(root_id)
        await self._require_agent_root(actor, agent, root, "read")
        return root

    async def _conversation_for_agent(
        self, actor: UserContext, agent_id: str, conversation_id: str
    ):
        agent = await self._usable_agent(agent_id, actor)
        conversation = await self.executions.get_conversation(conversation_id)
        if conversation is None or conversation.agent_id != agent.id:
            # Cross-agent IDs are indistinguishable from missing resources.
            raise NotFoundError("Conversation not found for this agent")
        if not actor.is_superuser and conversation.user_id != actor.user_id:
            await self.permissions.require_resource_access(
                actor,
                resource_type="conversation",
                resource_id=conversation.id,
                action="read",
                owner_id=conversation.user_id,
            )
        return conversation

    async def get_conversation(
        self, actor: UserContext, agent_id: str, conversation_id: str
    ) -> ConversationOut:
        conversation = await self._conversation_for_agent(actor, agent_id, conversation_id)
        return ConversationOut.model_validate(conversation)

    async def list_messages(
        self,
        actor: UserContext,
        agent_id: str,
        conversation_id: str,
        *,
        limit: int = 500,
    ) -> list[MessageOut]:
        conversation = await self._conversation_for_agent(actor, agent_id, conversation_id)
        rows = await self.executions.list_messages(conversation.id, limit=limit)
        return [MessageOut.model_validate(row) for row in rows]

    # ----------------------------------------------------------- executions/tasks

    async def list_executions(
        self,
        actor: UserContext,
        agent_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ExecutionOut]:
        agent = await self._usable_agent(agent_id, actor)
        rows = await self.executions.list_executions(
            user_id=None if actor.is_superuser else actor.user_id,
            agent_id=agent.id,
            limit=limit,
            offset=offset,
        )
        return [ExecutionOut.model_validate(row) for row in rows]

    async def list_tasks(
        self, actor: UserContext, agent_id: str, *, limit: int = 100
    ) -> list[TaskOut]:
        agent = await self._usable_agent(agent_id, actor)
        rows = await self.executions.list_tasks(
            user_id=None if actor.is_superuser else actor.user_id,
            agent_id=agent.id,
            limit=limit,
        )
        return [TaskOut.model_validate(row) for row in rows]

    # ---------------------------------------------------------------- audit helpers

    async def _audit(
        self,
        actor: UserContext,
        action: str,
        resource_id: str,
        *,
        details: dict | None = None,
        result: str = "success",
    ) -> None:
        await AuditService(self.db).record(
            actor_id=actor.user_id,
            actor_username=actor.username,
            action=action,
            resource_type="file_root",
            resource_id=resource_id,
            result=result,
            details=details or {},
            commit=False,
        )

    # ---------------------------------------------------------------- file roots

    async def list_agent_roots(
        self, actor: UserContext, agent_id: str
    ) -> list[FileRootAccessOut]:
        agent = await self._usable_agent(agent_id, actor)
        result: list[FileRootAccessOut] = []
        for grant in await self.repo.list_agent_roots(agent.id):
            root = await self.repo.get_root(grant.root_id)
            if root is None:
                continue
            if not await self.permissions.can_access_resource(
                actor,
                resource_type="file_root",
                resource_id=root.id,
                action="read",
                owner_id=root.owner_id,
            ):
                continue
            user_can_write = await self.permissions.can_access_resource(
                actor,
                resource_type="file_root",
                resource_id=root.id,
                action="write",
                owner_id=root.owner_id,
            )
            permission = (
                "write" if grant.permission == "write" and user_can_write else "read"
            )
            result.append(
                FileRootAccessOut(
                    id=root.id,
                    name=root.name,
                    description=root.description,
                    permission=permission,  # type: ignore[arg-type]
                )
            )
        return result

    async def _root_or_404(self, root_id: str) -> FileRoot:
        root = await self.repo.get_root(root_id)
        if root is None:
            raise NotFoundError("File root not found")
        return root

    @staticmethod
    def _require_root_manager(actor: UserContext, root: FileRoot) -> None:
        if actor.is_superuser or root.owner_id == actor.user_id:
            return
        # Other owners' roots are invisible to this administrator.
        raise NotFoundError("File root not found")

    async def _require_agent_root(
        self, actor: UserContext, agent: Agent, root: FileRoot, action: str
    ) -> AgentFileRoot:
        grant = await self.repo.get_agent_root(agent.id, root.id)
        if grant is None:
            # Unauthorized roots are indistinguishable from missing ones.
            raise NotFoundError("File root not found")
        allowed = await self.permissions.can_access_resource(
            actor,
            resource_type="file_root",
            resource_id=root.id,
            action=action,
            owner_id=root.owner_id,
        )
        if not allowed:
            raise NotFoundError("File root not found")
        if action == "write" and grant.permission != "write":
            raise PermissionDeniedError("Agent has read-only access to this file root")
        return grant

    def _storage_base(self) -> Path:
        return Path(get_settings().storage_dir).resolve()

    def _root_path(self, root: FileRoot) -> Path:
        base = self._storage_base()
        try:
            path = (base / root.storage_path).resolve()
            path.relative_to(base)
        except ValueError as exc:
            raise ValidationError("Invalid file root storage path") from exc
        return path

    @staticmethod
    def _relative_path(raw: str | None) -> PurePosixPath:
        value = (raw or "").strip()
        if "\x00" in value:
            raise ValidationError("Invalid path")
        value = value.replace("\\", "/")
        if value.startswith("/"):
            raise ValidationError("Absolute paths are not allowed")
        path = PurePosixPath(value)
        if path.is_absolute():
            raise ValidationError("Absolute paths are not allowed")
        parts = [part for part in path.parts if part not in {"", "."}]
        if any(part == ".." for part in parts):
            raise ValidationError("Path traversal is not allowed")
        return PurePosixPath(*parts) if parts else PurePosixPath(".")

    def _safe_target(self, root_path: Path, relative: PurePosixPath) -> Path:
        try:
            target = (root_path / Path(*relative.parts)).resolve()
            target.relative_to(root_path)
        except (OSError, ValueError) as exc:
            raise ValidationError("Path escapes the file root") from exc
        return target

    def _safe_entry(self, root_path: Path, relative: PurePosixPath) -> Path:
        """Containment-checked path that still refers to the directory entry.

        Unlike :meth:`_safe_target` this does not follow a final symlink, so
        deleting a link removes the link instead of its resolved target.
        """

        entry = root_path / Path(*relative.parts)
        try:
            entry.resolve().relative_to(root_path)
        except (OSError, ValueError) as exc:
            raise ValidationError("Path escapes the file root") from exc
        return entry

    async def list_entries(
        self,
        actor: UserContext,
        agent_id: str,
        root_id: str,
        *,
        path: str | None = None,
    ) -> list[FileRootEntryOut]:
        agent = await self._usable_agent(agent_id, actor)
        root = await self._root_or_404(root_id)
        await self._require_agent_root(actor, agent, root, "read")
        relative = self._relative_path(path)
        root_path = self._root_path(root)
        target = self._safe_target(root_path, relative)
        if not target.exists():
            raise NotFoundError("Path not found")
        if not target.is_dir():
            raise ValidationError("Path is not a directory")

        entries: list[FileRootEntryOut] = []
        for child in sorted(target.iterdir(), key=lambda item: item.name):
            try:
                resolved = child.resolve()
                resolved.relative_to(root_path)
            except (OSError, ValueError):
                # Never expose symlinks that escape the root.
                continue
            if resolved == root_path:
                continue
            stat = resolved.stat()
            entries.append(
                FileRootEntryOut(
                    name=child.name,
                    path=child.relative_to(root_path).as_posix(),
                    type="directory" if resolved.is_dir() else "file",
                    size=0 if resolved.is_dir() else stat.st_size,
                    modified_at=datetime.fromtimestamp(stat.st_mtime, UTC),
                )
            )
        return entries

    async def file_path(
        self, actor: UserContext, agent_id: str, root_id: str, *, path: str | None = None
    ) -> Path:
        agent = await self._usable_agent(agent_id, actor)
        root = await self._root_or_404(root_id)
        await self._require_agent_root(actor, agent, root, "read")
        relative = self._relative_path(path)
        target = self._safe_target(self._root_path(root), relative)
        if not target.exists():
            raise NotFoundError("File not found")
        if not target.is_file():
            raise ValidationError("Path is not a file")
        return target

    async def upload(
        self,
        actor: UserContext,
        agent_id: str,
        root_id: str,
        upload: UploadFile,
        *,
        path: str,
    ) -> FileRootEntryOut:
        agent = await self._usable_agent(agent_id, actor)
        root = await self._root_or_404(root_id)
        await self._require_agent_root(actor, agent, root, "write")
        relative = self._relative_path(path)
        if relative == PurePosixPath("."):
            raise ValidationError("An upload path is required")
        root_path = self._root_path(root)
        target = self._safe_entry(root_path, relative)
        if target.is_symlink():
            raise ValidationError("Upload target cannot be a symbolic link")
        if target.exists() and target.is_dir():
            raise ValidationError("Upload path is a directory")

        max_bytes = get_settings().max_upload_mb * 1024 * 1024
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await upload.read(_CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValidationError(
                    f"File exceeds the {get_settings().max_upload_mb} MB limit"
                )
            chunks.append(chunk)
        data = b"".join(chunks)

        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_bytes, data)
        stat = target.stat()
        return FileRootEntryOut(
            name=target.name,
            path=relative.as_posix(),
            type="file",
            size=stat.st_size,
            modified_at=datetime.fromtimestamp(stat.st_mtime, UTC),
        )

    async def delete_file(
        self, actor: UserContext, agent_id: str, root_id: str, *, path: str | None = None
    ) -> None:
        agent = await self._usable_agent(agent_id, actor)
        root = await self._root_or_404(root_id)
        await self._require_agent_root(actor, agent, root, "write")
        relative = self._relative_path(path)
        if relative == PurePosixPath("."):
            raise ValidationError("A file path is required")
        entry = self._safe_entry(self._root_path(root), relative)
        if not entry.exists() and not entry.is_symlink():
            raise NotFoundError("File not found")
        if entry.is_dir():
            raise ValidationError("Directories cannot be deleted through this endpoint")
        await asyncio.to_thread(entry.unlink)
        await self._audit(
            actor,
            "file_root.file.delete",
            root.id,
            details={
                "agent_id": agent.id,
                "path": relative.as_posix(),
                "filename": entry.name,
            },
        )
        await self.db.commit()

    # ------------------------------------------------------- admin: file roots

    async def create_root(self, actor: UserContext, payload: FileRootCreate) -> FileRootOut:
        await self.permissions.require_permission(actor, "agents:write")
        if await self.repo.get_root_by_name(payload.name):
            raise ConflictError("File root name already exists")
        root_id = new_id()
        storage_path = f"file_roots/{root_id}"
        base = self._storage_base()
        path = (base / storage_path).resolve()
        try:
            path.relative_to(base)
        except ValueError as exc:  # pragma: no cover - defensive
            raise ValidationError("Invalid file root storage path") from exc
        await asyncio.to_thread(path.mkdir, parents=True, exist_ok=True)
        root = FileRoot(
            id=root_id,
            name=payload.name,
            description=payload.description,
            owner_id=actor.user_id,
            storage_path=storage_path,
        )
        self.db.add(root)
        await self._audit(
            actor,
            "file_root.create",
            root_id,
            details={"name": payload.name, "storage_path": storage_path},
        )
        await self.db.commit()
        await self.db.refresh(root)
        return FileRootOut.model_validate(root)

    async def list_roots(self, actor: UserContext) -> list[FileRootOut]:
        await self.permissions.require_permission(actor, "agents:write")
        roots = await self.repo.list_roots()
        if not actor.is_superuser:
            roots = [root for root in roots if root.owner_id == actor.user_id]
        return [FileRootOut.model_validate(root) for root in roots]

    async def list_root_grants(
        self, actor: UserContext, root_id: str
    ) -> list[FileRootGrantOut]:
        await self.permissions.require_permission(actor, "agents:write")
        root = await self._root_or_404(root_id)
        self._require_root_manager(actor, root)
        agents = AgentRepository(self.db)
        result: list[FileRootGrantOut] = []
        for grant in await self.repo.list_root_grants(root_id):
            agent = await agents.get_agent(grant.agent_id)
            item = FileRootGrantOut.model_validate(grant)
            item.agent_name = agent.name if agent else ""
            result.append(item)
        return result

    async def grant_agent(
        self,
        actor: UserContext,
        root_id: str,
        payload: FileRootGrantCreate,
    ) -> FileRootGrantOut:
        await self.permissions.require_permission(actor, "agents:write")
        root = await self._root_or_404(root_id)
        self._require_root_manager(actor, root)
        agent = await AgentRepository(self.db).get_agent(payload.agent_id)
        if agent is None:
            raise NotFoundError("Agent not found")
        grant = await self.repo.upsert_agent_root(
            agent_id=agent.id, root_id=root.id, permission=payload.permission
        )
        await self._audit(
            actor,
            "file_root.grant",
            root.id,
            details={
                "agent_id": agent.id,
                "agent_name": agent.name,
                "permission": payload.permission,
            },
        )
        await self.db.commit()
        await self.db.refresh(grant)
        item = FileRootGrantOut.model_validate(grant)
        item.agent_name = agent.name
        return item

    async def revoke_agent(self, actor: UserContext, root_id: str, agent_id: str) -> None:
        await self.permissions.require_permission(actor, "agents:write")
        root = await self._root_or_404(root_id)
        self._require_root_manager(actor, root)
        grant = await self.repo.get_agent_root(agent_id, root.id)
        if grant is None:
            raise NotFoundError("Agent is not authorized for this file root")
        await self.repo.delete_agent_root(grant)
        await self._audit(
            actor,
            "file_root.revoke",
            root.id,
            details={"agent_id": agent_id},
        )
        await self.db.commit()


__all__ = ["WorkspaceService"]
