from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.prompt.models import Prompt, PromptVersion


class PromptRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_prompts(self) -> list[Prompt]:
        return list((await self.db.scalars(select(Prompt).order_by(Prompt.name))).all())

    async def get_prompt(self, prompt_id: str) -> Prompt | None:
        return await self.db.get(Prompt, prompt_id)

    async def get_prompt_by_name(self, name: str) -> Prompt | None:
        return await self.db.scalar(select(Prompt).where(Prompt.name == name))

    async def list_versions(self, prompt_id: str) -> list[PromptVersion]:
        return list(
            (
                await self.db.scalars(
                    select(PromptVersion)
                    .where(PromptVersion.prompt_id == prompt_id)
                    .order_by(PromptVersion.version)
                )
            ).all()
        )

    async def get_version(self, version_id: str) -> PromptVersion | None:
        return await self.db.get(PromptVersion, version_id)

    async def get_version_by_number(self, prompt_id: str, version: int) -> PromptVersion | None:
        return await self.db.scalar(
            select(PromptVersion).where(
                PromptVersion.prompt_id == prompt_id, PromptVersion.version == version
            )
        )

    async def next_version_number(self, prompt_id: str) -> int:
        current = await self.db.scalar(
            select(func.max(PromptVersion.version)).where(PromptVersion.prompt_id == prompt_id)
        )
        return int(current or 0) + 1
