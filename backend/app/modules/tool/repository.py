from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.tool.models import Tool, ToolExecution, ToolPermission, ToolVersion


class ToolRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_tools(self) -> list[Tool]:
        return list((await self.db.scalars(select(Tool).order_by(Tool.name))).all())

    async def get(self, tool_id: str) -> Tool | None:
        return await self.db.get(Tool, tool_id)

    async def get_by_name(self, name: str) -> Tool | None:
        return await self.db.scalar(select(Tool).where(Tool.name == name))

    async def get_many_by_names(self, names: Sequence[str]) -> dict[str, Tool]:
        if not names:
            return {}
        rows = await self.db.scalars(select(Tool).where(Tool.name.in_(list(names))))
        return {row.name: row for row in rows}

    async def latest_version(self, tool_id: str) -> ToolVersion | None:
        return await self.db.scalar(
            select(ToolVersion)
            .where(ToolVersion.tool_id == tool_id)
            .order_by(ToolVersion.version.desc())
            .limit(1)
        )

    async def latest_versions(self) -> dict[str, int]:
        rows = await self.db.execute(
            select(ToolVersion.tool_id, func.max(ToolVersion.version)).group_by(
                ToolVersion.tool_id
            )
        )
        return {tool_id: version for tool_id, version in rows.all()}

    def add_version(self, version: ToolVersion) -> None:
        self.db.add(version)

    async def grants_for_tool(self, tool_id: str) -> list[ToolPermission]:
        return list(
            (
                await self.db.scalars(
                    select(ToolPermission).where(ToolPermission.tool_id == tool_id)
                )
            ).all()
        )

    async def grants_for_tools(self, tool_ids: Sequence[str]) -> dict[str, list[ToolPermission]]:
        if not tool_ids:
            return {}
        rows = await self.db.scalars(
            select(ToolPermission).where(ToolPermission.tool_id.in_(list(tool_ids)))
        )
        grouped: dict[str, list[ToolPermission]] = {}
        for row in rows:
            grouped.setdefault(row.tool_id, []).append(row)
        return grouped

    def record_execution(self, execution: ToolExecution) -> None:
        self.db.add(execution)

    async def list_execution_records(self, execution_id: str) -> list[ToolExecution]:
        return list(
            (
                await self.db.scalars(
                    select(ToolExecution)
                    .where(ToolExecution.execution_id == execution_id)
                    .order_by(ToolExecution.created_at, ToolExecution.id)
                )
            ).all()
        )
