from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.workspace.models import AgentFileRoot, FileRoot


class WorkspaceRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ----------------------------------------------------------------- file roots

    async def get_root(self, root_id: str) -> FileRoot | None:
        return await self.db.get(FileRoot, root_id)

    async def get_root_by_name(self, name: str) -> FileRoot | None:
        return await self.db.scalar(select(FileRoot).where(FileRoot.name == name))

    async def list_roots(self) -> list[FileRoot]:
        return list((await self.db.scalars(select(FileRoot).order_by(FileRoot.name))).all())

    async def create_root(self, root: FileRoot) -> FileRoot:
        self.db.add(root)
        await self.db.flush()
        return root

    # ---------------------------------------------------------- agent root grants

    async def get_agent_root(self, agent_id: str, root_id: str) -> AgentFileRoot | None:
        return await self.db.scalar(
            select(AgentFileRoot).where(
                AgentFileRoot.agent_id == agent_id,
                AgentFileRoot.root_id == root_id,
            )
        )

    async def list_agent_roots(self, agent_id: str) -> list[AgentFileRoot]:
        return list(
            (
                await self.db.scalars(
                    select(AgentFileRoot)
                    .where(AgentFileRoot.agent_id == agent_id)
                    .order_by(AgentFileRoot.created_at)
                )
            ).all()
        )

    async def list_root_grants(self, root_id: str) -> list[AgentFileRoot]:
        return list(
            (
                await self.db.scalars(
                    select(AgentFileRoot)
                    .where(AgentFileRoot.root_id == root_id)
                    .order_by(AgentFileRoot.created_at)
                )
            ).all()
        )

    async def delete_agent_root(self, grant: AgentFileRoot) -> None:
        await self.db.delete(grant)
        await self.db.flush()

    async def upsert_agent_root(
        self, *, agent_id: str, root_id: str, permission: str
    ) -> AgentFileRoot:
        grant = await self.get_agent_root(agent_id, root_id)
        if grant is None:
            grant = AgentFileRoot(
                agent_id=agent_id, root_id=root_id, permission=permission
            )
            self.db.add(grant)
        else:
            grant.permission = permission
        await self.db.flush()
        return grant
