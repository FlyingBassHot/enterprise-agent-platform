from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.skill.models import Skill, SkillDependency, SkillVersion


class SkillRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_skills(self) -> list[Skill]:
        return list((await self.db.scalars(select(Skill).order_by(Skill.name))).all())

    async def get(self, skill_id: str) -> Skill | None:
        return await self.db.get(Skill, skill_id)

    async def get_by_name(self, name: str) -> Skill | None:
        return await self.db.scalar(select(Skill).where(Skill.name == name))

    async def get_many_by_names(self, names: Sequence[str]) -> dict[str, Skill]:
        if not names:
            return {}
        rows = await self.db.scalars(select(Skill).where(Skill.name.in_(list(names))))
        return {row.name: row for row in rows}

    async def latest_version(self, skill_id: str) -> SkillVersion | None:
        return await self.db.scalar(
            select(SkillVersion)
            .where(SkillVersion.skill_id == skill_id)
            .order_by(SkillVersion.version.desc())
            .limit(1)
        )

    async def latest_versions(self) -> dict[str, SkillVersion]:
        rows = await self.db.scalars(
            select(SkillVersion).order_by(SkillVersion.skill_id, SkillVersion.version)
        )
        latest: dict[str, SkillVersion] = {}
        for row in rows:
            latest[row.skill_id] = row
        return latest

    async def versions_for(self, skill_ids: Sequence[str]) -> dict[str, SkillVersion]:
        if not skill_ids:
            return {}
        rows = await self.db.scalars(
            select(SkillVersion)
            .where(SkillVersion.skill_id.in_(list(skill_ids)))
            .order_by(SkillVersion.skill_id, SkillVersion.version)
        )
        latest: dict[str, SkillVersion] = {}
        for row in rows:
            latest[row.skill_id] = row
        return latest

    async def list_versions(self, skill_id: str) -> list[SkillVersion]:
        return list(
            (
                await self.db.scalars(
                    select(SkillVersion)
                    .where(SkillVersion.skill_id == skill_id)
                    .order_by(SkillVersion.version)
                )
            ).all()
        )

    async def list_dependencies(self, skill_id: str) -> list[SkillDependency]:
        return list(
            (
                await self.db.scalars(
                    select(SkillDependency)
                    .where(SkillDependency.skill_id == skill_id)
                    .order_by(SkillDependency.kind, SkillDependency.ref)
                )
            ).all()
        )

    def add_version(self, version: SkillVersion) -> None:
        self.db.add(version)

    def add_dependency(self, dependency: SkillDependency) -> None:
        self.db.add(dependency)
