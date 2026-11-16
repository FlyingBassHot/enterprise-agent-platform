"""Task-scoped plan state.

A task is what the user asked for; executions are attempts at it. Plan steps
live on the task, so an exhausted or failed attempt can be continued by a new
execution without losing the plan, and context compaction cannot drop it.

The runtime only depends on ``app.core.contracts.TaskGateway``; this module is
the concrete implementation used by the execution module.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.contracts import UserContext
from app.core.exceptions import NotFoundError, PermissionDeniedError, ValidationError
from app.core.ids import new_id
from app.core.plan import normalize_plan
from app.modules.execution.models import AgentTask, TaskStep
from app.modules.execution.repository import ExecutionRepository
from app.modules.execution.schemas import TaskDetailOut, TaskOut, TaskStepOut
from app.modules.permission.service import PermissionService

MAX_GOAL_CHARS = 2000


class TaskService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = ExecutionRepository(db)
        self.permissions = PermissionService(db)

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _require(actor: UserContext, *codes: str) -> None:
        missing = [code for code in codes if not actor.has(code)]
        if missing:
            raise PermissionDeniedError(f"Missing permission: {', '.join(missing)}")

    async def _require_task_read(self, actor: UserContext, task: AgentTask) -> None:
        if actor.is_superuser or task.user_id == actor.user_id:
            return
        await self.permissions.require_resource_access(
            actor,
            resource_type="task",
            resource_id=task.id,
            action="read",
            owner_id=task.user_id,
        )

    async def _require_task_write(self, actor: UserContext, task: AgentTask) -> None:
        if actor.is_superuser or task.user_id == actor.user_id:
            return
        await self.permissions.require_resource_access(
            actor,
            resource_type="task",
            resource_id=task.id,
            action="write",
            owner_id=task.user_id,
        )

    async def _load(self, task_id: str) -> AgentTask:
        task = await self.repo.get_task(task_id)
        if task is None:
            raise NotFoundError(f"Task {task_id} not found")
        return task

    @staticmethod
    def _slim(step: TaskStep) -> dict[str, str]:
        return {"id": step.step_key, "title": step.title, "status": step.status}

    # ------------------------------------------------------------------- public

    async def ensure_task(
        self,
        *,
        agent_id: str,
        user_id: str,
        conversation_id: str | None,
        goal: str,
    ) -> AgentTask:
        task = AgentTask(
            id=new_id(),
            agent_id=agent_id,
            user_id=user_id,
            conversation_id=conversation_id,
            goal=(goal or "").strip()[:MAX_GOAL_CHARS],
            constraints={},
            success_criteria=[],
            status="active",
        )
        return await self.repo.create_task(task)

    async def resolve_for_execution(
        self, actor: UserContext, task_id: str, conversation_id: str | None
    ) -> AgentTask:
        """Authorize continuing an existing task from a new execution."""

        self._require(actor, "executions:write")
        task = await self._load(task_id)
        await self._require_task_write(actor, task)
        if conversation_id and task.conversation_id not in {None, conversation_id}:
            raise ValidationError("Task belongs to a different conversation")
        return task

    async def get_task(self, actor: UserContext, task_id: str) -> TaskDetailOut:
        self._require(actor, "executions:read")
        task = await self._load(task_id)
        await self._require_task_read(actor, task)
        steps = await self.repo.list_steps(task.id)
        detail = TaskDetailOut.model_validate(task)
        detail.steps = [TaskStepOut.model_validate(step) for step in steps]
        return detail

    async def list_tasks(
        self,
        actor: UserContext,
        *,
        conversation_id: str | None = None,
        limit: int = 100,
    ) -> list[TaskOut]:
        self._require(actor, "executions:read")
        rows = await self.repo.list_tasks(
            user_id=None if actor.is_superuser else actor.user_id,
            conversation_id=conversation_id,
            limit=limit,
        )
        return [TaskOut.model_validate(row) for row in rows]

    async def steps_for_task(self, task_id: str) -> list[TaskStep]:
        return await self.repo.list_steps(task_id)

    # ------------------------------------------------------- TaskGateway contract

    async def steps_for_execution(self, execution_id: str) -> list[dict[str, str]]:
        execution = await self.repo.get_execution(execution_id)
        if execution is None or not execution.task_id:
            return []
        rows = await self.repo.list_steps(execution.task_id)
        return [self._slim(step) for step in rows]

    async def apply_plan(
        self, execution_id: str, plan: Sequence[dict[str, str]]
    ) -> None:
        execution = await self.repo.get_execution(execution_id)
        if execution is None or not execution.task_id:
            return
        await self.apply_plan_to_task(execution.task_id, plan)

    async def apply_plan_to_task(
        self, task_id: str, plan: Sequence[dict[str, str]]
    ) -> list[TaskStep]:
        """Upsert steps by stable key; steps dropped by the model are removed."""

        normalized = normalize_plan(list(plan))
        if normalized is None:
            raise ValidationError("Invalid plan payload")
        existing = {step.step_key: step for step in await self.repo.list_steps(task_id)}
        kept: set[str] = set()
        for position, item in enumerate(normalized):
            key = item["id"]
            kept.add(key)
            step = existing.get(key)
            if step is None:
                step = TaskStep(
                    id=new_id(),
                    task_id=task_id,
                    step_key=key,
                    position=position,
                    title=item["title"],
                    status=item["status"],
                )
                self.db.add(step)
            else:
                step.position = position
                step.title = item["title"]
                step.status = item["status"]
        for key, step in existing.items():
            if key not in kept:
                await self.db.delete(step)
        await self.db.commit()
        return await self.repo.list_steps(task_id)


__all__ = ["TaskService"]
