from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.workflow.models import Workflow, WorkflowExecution, WorkflowVersion


class WorkflowRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_workflows(self) -> list[Workflow]:
        return list(
            (await self.db.scalars(select(Workflow).order_by(Workflow.name))).all()
        )

    async def get_workflow(self, workflow_id: str) -> Workflow | None:
        return await self.db.get(Workflow, workflow_id)

    async def get_workflow_by_name(self, name: str) -> Workflow | None:
        return await self.db.scalar(select(Workflow).where(Workflow.name == name))

    async def list_versions(self, workflow_id: str) -> list[WorkflowVersion]:
        return list(
            (
                await self.db.scalars(
                    select(WorkflowVersion)
                    .where(WorkflowVersion.workflow_id == workflow_id)
                    .order_by(WorkflowVersion.version)
                )
            ).all()
        )

    async def get_version(self, version_id: str) -> WorkflowVersion | None:
        return await self.db.get(WorkflowVersion, version_id)

    async def next_version_number(self, workflow_id: str) -> int:
        current = await self.db.scalar(
            select(func.max(WorkflowVersion.version)).where(
                WorkflowVersion.workflow_id == workflow_id
            )
        )
        return int(current or 0) + 1

    def add_execution(self, execution: WorkflowExecution) -> None:
        self.db.add(execution)

    async def get_execution_by_execution_id(
        self, execution_id: str
    ) -> WorkflowExecution | None:
        return await self.db.scalar(
            select(WorkflowExecution).where(
                WorkflowExecution.execution_id == execution_id
            )
        )
