from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import new_id
from app.modules.execution.models import (
    AgentExecution,
    AgentTask,
    ApprovalRequest,
    Artifact,
    Conversation,
    ExecutionEvent,
    LLMCall,
    Message,
    TaskStep,
)


class ExecutionRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ------------------------------------------------------------ conversations

    async def create_conversation(
        self, *, agent_id: str, user_id: str, title: str = "", conversation_id: str | None = None
    ) -> Conversation:
        row = Conversation(
            id=conversation_id or new_id(), agent_id=agent_id, user_id=user_id, title=title
        )
        self.db.add(row)
        await self.db.flush()
        return row

    async def get_conversation(self, conversation_id: str) -> Conversation | None:
        return await self.db.get(Conversation, conversation_id)

    async def list_conversations(
        self,
        *,
        user_id: str | None = None,
        agent_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Conversation]:
        stmt = select(Conversation)
        if user_id:
            stmt = stmt.where(Conversation.user_id == user_id)
        if agent_id:
            stmt = stmt.where(Conversation.agent_id == agent_id)
        stmt = stmt.order_by(Conversation.updated_at.desc()).limit(limit).offset(offset)
        return list((await self.db.execute(stmt)).scalars().all())

    # ----------------------------------------------------------------- messages

    async def add_message(
        self,
        *,
        conversation_id: str,
        role: str,
        content: str,
        tool_calls: list[Any] | None = None,
        execution_id: str | None = None,
    ) -> Message:
        row = Message(
            conversation_id=conversation_id,
            role=role,
            content=content,
            tool_calls=tool_calls,
            execution_id=execution_id,
        )
        self.db.add(row)
        await self.db.flush()
        return row

    async def list_messages(
        self,
        conversation_id: str,
        *,
        limit: int = 500,
        before_execution_id: str | None = None,
    ) -> list[Message]:
        stmt = select(Message).where(Message.conversation_id == conversation_id)
        if before_execution_id:
            stmt = stmt.where(
                (Message.execution_id.is_(None)) | (Message.execution_id != before_execution_id)
            )
        stmt = (
            stmt.order_by(Message.created_at.desc(), Message.id.desc()).limit(limit)
        )
        rows = list((await self.db.execute(stmt)).scalars().all())
        rows.reverse()
        return rows

    # --------------------------------------------------------------- executions

    async def create_execution(self, execution: AgentExecution) -> AgentExecution:
        self.db.add(execution)
        await self.db.flush()
        return execution

    async def get_execution(self, execution_id: str) -> AgentExecution | None:
        return await self.db.get(AgentExecution, execution_id)

    async def list_executions(
        self,
        *,
        user_id: str | None = None,
        agent_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AgentExecution]:
        stmt = select(AgentExecution)
        if user_id:
            stmt = stmt.where(AgentExecution.user_id == user_id)
        if agent_id:
            stmt = stmt.where(AgentExecution.agent_id == agent_id)
        if status:
            stmt = stmt.where(AgentExecution.status == status)
        stmt = (
            stmt.order_by(AgentExecution.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list((await self.db.execute(stmt)).scalars().all())

    async def list_running_executions(self) -> list[AgentExecution]:
        stmt = select(AgentExecution).where(AgentExecution.status == "running")
        return list((await self.db.execute(stmt)).scalars().all())

    # ------------------------------------------------------------------- events

    async def append_event(
        self, execution_id: str, event_type: str, payload: dict[str, Any] | None = None
    ) -> ExecutionEvent:
        row = ExecutionEvent(execution_id=execution_id, type=event_type, payload=payload or {})
        self.db.add(row)
        await self.db.flush()
        return row

    async def list_events(
        self, execution_id: str, *, after_seq: int = 0, limit: int = 500
    ) -> list[ExecutionEvent]:
        stmt = (
            select(ExecutionEvent)
            .where(ExecutionEvent.execution_id == execution_id, ExecutionEvent.seq > after_seq)
            .order_by(ExecutionEvent.seq.asc())
            .limit(limit)
        )
        return list((await self.db.execute(stmt)).scalars().all())

    async def count_events(self, execution_id: str) -> int:
        stmt = select(func.count()).select_from(ExecutionEvent).where(
            ExecutionEvent.execution_id == execution_id
        )
        return int((await self.db.execute(stmt)).scalar_one())

    # ---------------------------------------------------------------- approvals

    async def create_approval(self, approval: ApprovalRequest) -> ApprovalRequest:
        self.db.add(approval)
        await self.db.flush()
        return approval

    async def get_approval(self, approval_id: str) -> ApprovalRequest | None:
        return await self.db.get(ApprovalRequest, approval_id)

    async def get_approval_by_call(
        self, execution_id: str, tool_call_id: str
    ) -> ApprovalRequest | None:
        stmt = (
            select(ApprovalRequest)
            .where(
                ApprovalRequest.execution_id == execution_id,
                ApprovalRequest.tool_call_id == tool_call_id,
            )
            .order_by(ApprovalRequest.requested_at.desc())
            .limit(1)
        )
        return (await self.db.execute(stmt)).scalars().first()

    async def get_approval_by_key(
        self, execution_id: str, approval_key: str
    ) -> ApprovalRequest | None:
        stmt = (
            select(ApprovalRequest)
            .where(
                ApprovalRequest.execution_id == execution_id,
                ApprovalRequest.approval_key == approval_key,
            )
            .order_by(ApprovalRequest.requested_at.desc())
            .limit(1)
        )
        return (await self.db.execute(stmt)).scalars().first()

    async def list_approvals(
        self,
        *,
        execution_id: str | None = None,
        status: str | None = None,
        owner_id: str | None = None,
        limit: int = 200,
    ) -> list[ApprovalRequest]:
        stmt = select(ApprovalRequest)
        if owner_id:
            stmt = stmt.join(
                AgentExecution, AgentExecution.id == ApprovalRequest.execution_id
            ).where(AgentExecution.user_id == owner_id)
        if execution_id:
            stmt = stmt.where(ApprovalRequest.execution_id == execution_id)
        if status:
            stmt = stmt.where(ApprovalRequest.status == status)
        stmt = stmt.order_by(ApprovalRequest.requested_at.asc()).limit(limit)
        return list((await self.db.execute(stmt)).scalars().all())

    async def list_pending_approvals(self, execution_id: str) -> list[ApprovalRequest]:
        return await self.list_approvals(execution_id=execution_id, status="pending")

    async def list_resolved_approvals(self, execution_id: str) -> list[ApprovalRequest]:
        stmt = (
            select(ApprovalRequest)
            .where(
                ApprovalRequest.execution_id == execution_id,
                ApprovalRequest.status != "pending",
            )
            .order_by(ApprovalRequest.resolved_at.desc())
        )
        return list((await self.db.execute(stmt)).scalars().all())

    # --------------------------------------------------------------- llm calls

    async def add_llm_call(self, call: LLMCall) -> LLMCall:
        self.db.add(call)
        await self.db.flush()
        return call

    async def list_llm_calls(self, execution_id: str) -> list[LLMCall]:
        stmt = (
            select(LLMCall)
            .where(LLMCall.execution_id == execution_id)
            .order_by(LLMCall.created_at.asc())
        )
        return list((await self.db.execute(stmt)).scalars().all())

    # ---------------------------------------------------------------- artifacts

    async def sum_artifact_size(self, owner_id: str) -> int:
        from sqlalchemy import func

        return int(
            await self.db.scalar(
                select(func.coalesce(func.sum(Artifact.size), 0)).where(
                    Artifact.owner_id == owner_id
                )
            )
            or 0
        )

    async def create_artifact(self, artifact: Artifact) -> Artifact:
        self.db.add(artifact)
        await self.db.flush()
        return artifact

    async def get_artifact(self, artifact_id: str) -> Artifact | None:
        return await self.db.get(Artifact, artifact_id)

    async def list_artifacts(self, ids: Sequence[str]) -> list[Artifact]:
        if not ids:
            return []
        stmt = select(Artifact).where(Artifact.id.in_(list(ids)))
        return list((await self.db.execute(stmt)).scalars().all())

    # -------------------------------------------------------------------- tasks

    async def create_task(self, task: AgentTask) -> AgentTask:
        self.db.add(task)
        await self.db.flush()
        return task

    async def get_task(self, task_id: str) -> AgentTask | None:
        return await self.db.get(AgentTask, task_id)

    async def list_tasks(
        self,
        *,
        user_id: str | None = None,
        conversation_id: str | None = None,
        limit: int = 100,
    ) -> list[AgentTask]:
        stmt = select(AgentTask)
        if user_id:
            stmt = stmt.where(AgentTask.user_id == user_id)
        if conversation_id:
            stmt = stmt.where(AgentTask.conversation_id == conversation_id)
        stmt = stmt.order_by(AgentTask.created_at.desc()).limit(limit)
        return list((await self.db.execute(stmt)).scalars().all())

    async def list_steps(self, task_id: str) -> list[TaskStep]:
        stmt = (
            select(TaskStep)
            .where(TaskStep.task_id == task_id)
            .order_by(TaskStep.position.asc(), TaskStep.created_at.asc())
        )
        return list((await self.db.execute(stmt)).scalars().all())
