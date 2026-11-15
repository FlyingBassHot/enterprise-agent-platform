from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.agent.models import Agent, AgentVersion


class AgentRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_agents(self) -> list[Agent]:
        return list((await self.db.scalars(select(Agent).order_by(Agent.name))).all())

    async def get_agent(self, agent_id: str) -> Agent | None:
        return await self.db.get(Agent, agent_id)

    async def get_agent_by_name(self, name: str) -> Agent | None:
        return await self.db.scalar(select(Agent).where(Agent.name == name))

    async def list_versions(self, agent_id: str) -> list[AgentVersion]:
        return list(
            (
                await self.db.scalars(
                    select(AgentVersion)
                    .where(AgentVersion.agent_id == agent_id)
                    .order_by(AgentVersion.version)
                )
            ).all()
        )

    async def get_version(self, version_id: str) -> AgentVersion | None:
        return await self.db.get(AgentVersion, version_id)

    async def next_version_number(self, agent_id: str) -> int:
        current = await self.db.scalar(
            select(func.max(AgentVersion.version)).where(AgentVersion.agent_id == agent_id)
        )
        return int(current or 0) + 1
