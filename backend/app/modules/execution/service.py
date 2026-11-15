"""Execution orchestration: lifecycle, conversations, events, approvals."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.contracts import (
    AgentLoader,
    AgentSnapshot,
    ChatMessage,
    KnowledgeGateway,
    ModelGateway,
    SkillGateway,
    ToolGateway,
    UserContext,
)
from app.core.enums import TERMINAL_EXECUTION_STATUSES, ExecutionStatus
from app.core.exceptions import (
    ConflictError,
    ExecutionError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from app.core.ids import new_id
from app.core.logging import get_logger, request_id_var
from app.infrastructure.database.session import get_session_factory
from app.modules.execution.events import ExecutionEventBroker, ExecutionEventSink, get_broker
from app.modules.execution.models import AgentExecution
from app.modules.execution.repository import ExecutionRepository
from app.modules.execution.schemas import (
    ApprovalOut,
    ConversationCreate,
    ConversationOut,
    ExecutionCreate,
    ExecutionDetailOut,
    ExecutionEventOut,
    ExecutionOut,
    LLMCallOut,
    MessageOut,
)
from app.modules.permission.service import PermissionService
from app.modules.runtime.context import render_user_text
from app.modules.runtime.service import RuntimeResult, RuntimeService

logger = get_logger(__name__)

_TASKS: dict[str, asyncio.Task[Any]] = {}


def _now() -> datetime:
    return datetime.now(UTC)


def _agent_service(db: AsyncSession) -> AgentLoader:
    from app.modules.agent.service import AgentService

    return AgentService(db)


def _tool_service(db: AsyncSession) -> ToolGateway:
    from app.modules.tool.service import ToolService

    return ToolService(db)


def _model_service(db: AsyncSession) -> ModelGateway:
    from app.modules.model.service import ModelService

    return ModelService(db)


class ExecutionService:
    def __init__(
        self,
        db: AsyncSession,
        *,
        agent_loader: AgentLoader | None = None,
        tool_gateway: ToolGateway | None = None,
        model_gateway: ModelGateway | None = None,
        skill_gateway: SkillGateway | None = None,
        knowledge_gateway: KnowledgeGateway | None = None,
        broker: ExecutionEventBroker | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        checkpointer: Any = None,
    ) -> None:
        self.db = db
        self.repo = ExecutionRepository(db)
        self._agent_loader = agent_loader
        self._tool_gateway = tool_gateway
        self._model_gateway = model_gateway
        self._skill_gateway = skill_gateway
        self._knowledge_gateway = knowledge_gateway
        self.broker = broker or get_broker()
        self.session_factory = session_factory or get_session_factory()
        self._checkpointer = checkpointer

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _require(user: UserContext, *codes: str) -> None:
        missing = [code for code in codes if not user.has(code)]
        if missing:
            raise PermissionDeniedError(f"Missing permission: {', '.join(missing)}")

    async def _require_execution_read(self, actor: UserContext, execution: AgentExecution) -> None:
        if actor.is_superuser or execution.user_id == actor.user_id:
            return
        await PermissionService(self.db).require_resource_access(
            actor,
            resource_type="execution",
            resource_id=execution.id,
            action="read",
            owner_id=execution.user_id,
        )

    async def _require_execution_write(self, actor: UserContext, execution: AgentExecution) -> None:
        if actor.is_superuser or execution.user_id == actor.user_id:
            return
        await PermissionService(self.db).require_resource_access(
            actor,
            resource_type="execution",
            resource_id=execution.id,
            action="write",
            owner_id=execution.user_id,
        )

    async def _load_execution(self, execution_id: str) -> AgentExecution:
        execution = await self.repo.get_execution(execution_id)
        if execution is None:
            raise NotFoundError(f"Execution {execution_id} not found")
        return execution

    async def _load_conversation(self, conversation: Any, actor: UserContext) -> None:
        if actor.is_superuser or conversation.user_id == actor.user_id:
            return
        await PermissionService(self.db).require_resource_access(
            actor,
            resource_type="conversation",
            resource_id=conversation.id,
            action="read",
            owner_id=conversation.user_id,
        )

    @staticmethod
    def _check_agent_access(actor: UserContext, snapshot: AgentSnapshot) -> None:
        if snapshot.required_permissions:
            missing = [code for code in snapshot.required_permissions if not actor.has(code)]
            if missing:
                raise PermissionDeniedError(
                    f"Missing agent required permission: {', '.join(missing)}"
                )
        if snapshot.allowed_roles and not actor.is_superuser:
            if not set(snapshot.allowed_roles).intersection(actor.roles):
                raise PermissionDeniedError("Your role is not allowed to run this agent")

    # -------------------------------------------------------------- conversations

    async def create_conversation(
        self, actor: UserContext, payload: ConversationCreate
    ) -> ConversationOut:
        self._require(actor, "executions:write")
        conversation = await self.repo.create_conversation(
            agent_id=payload.agent_id,
            user_id=actor.user_id,
            title=payload.title.strip() or "新对话",
        )
        await self.db.commit()
        return ConversationOut.model_validate(conversation)

    async def list_conversations(
        self, actor: UserContext, *, agent_id: str | None = None, limit: int = 100
    ) -> list[ConversationOut]:
        self._require(actor, "executions:read")
        rows = await self.repo.list_conversations(
            user_id=None if actor.is_superuser else actor.user_id,
            agent_id=agent_id,
            limit=limit,
        )
        return [ConversationOut.model_validate(row) for row in rows]

    async def get_conversation(self, actor: UserContext, conversation_id: str) -> ConversationOut:
        self._require(actor, "executions:read")
        conversation = await self.repo.get_conversation(conversation_id)
        if conversation is None:
            raise NotFoundError(f"Conversation {conversation_id} not found")
        await self._load_conversation(conversation, actor)
        return ConversationOut.model_validate(conversation)

    async def list_messages(
        self, actor: UserContext, conversation_id: str, *, limit: int = 500
    ) -> list[MessageOut]:
        self._require(actor, "executions:read")
        conversation = await self.repo.get_conversation(conversation_id)
        if conversation is None:
            raise NotFoundError(f"Conversation {conversation_id} not found")
        await self._load_conversation(conversation, actor)
        rows = await self.repo.list_messages(conversation_id, limit=limit)
        return [MessageOut.model_validate(row) for row in rows]

    async def _resolve_conversation(
        self, actor: UserContext, snapshot: AgentSnapshot, payload: ExecutionCreate
    ):
        if payload.conversation_id:
            conversation = await self.repo.get_conversation(payload.conversation_id)
            if conversation is None:
                raise NotFoundError(f"Conversation {payload.conversation_id} not found")
            if not actor.is_superuser and conversation.user_id != actor.user_id:
                raise PermissionDeniedError("Conversation belongs to another user")
            if conversation.agent_id != snapshot.agent_id:
                raise ConflictError("Conversation belongs to a different agent")
            return conversation
        title = (payload.input or snapshot.agent_name or "新对话").strip().splitlines()[0][:120]
        return await self.repo.create_conversation(
            agent_id=snapshot.agent_id, user_id=actor.user_id, title=title or "新对话"
        )

    # ------------------------------------------------------------------ artifacts

    async def _load_attachments(
        self, actor: UserContext, artifact_ids: Sequence[str]
    ) -> list[dict[str, str]]:
        if not artifact_ids:
            return []
        rows = await self.repo.list_artifacts(artifact_ids)
        found = {row.id: row for row in rows}
        attachments: list[dict[str, str]] = []
        for artifact_id in artifact_ids:
            artifact = found.get(artifact_id)
            if artifact is None:
                raise NotFoundError(f"Artifact {artifact_id} not found")
            if not actor.is_superuser and artifact.owner_id != actor.user_id:
                await PermissionService(self.db).require_resource_access(
                    actor,
                    resource_type="artifact",
                    resource_id=artifact.id,
                    action="read",
                    owner_id=artifact.owner_id,
                )
            attachments.append({"id": artifact.id, "filename": artifact.filename})
        return attachments

    # ---------------------------------------------------------------- create run

    async def create_execution(
        self, actor: UserContext, payload: ExecutionCreate
    ) -> ExecutionOut:
        self._require(actor, "agents:write", "executions:write")
        loader = self._agent_loader
        if loader is None:
            loader = _agent_service(self.db)
        snapshot = await loader.load_published(payload.agent_id)
        self._check_agent_access(actor, snapshot)

        conversation = await self._resolve_conversation(actor, snapshot, payload)
        attachments = await self._load_attachments(actor, payload.artifact_ids)
        user_text = render_user_text(payload.input, attachments)

        execution_id = new_id()
        trace_id = request_id_var.get()
        if not trace_id or trace_id == "-":
            trace_id = new_id()
        execution = AgentExecution(
            id=execution_id,
            agent_id=snapshot.agent_id,
            agent_version_id=snapshot.version_id,
            agent_version=snapshot.version,
            user_id=actor.user_id,
            conversation_id=conversation.id,
            mode=snapshot.mode or "agent",
            status=ExecutionStatus.PENDING.value,
            input_text=user_text,
            input_payload={
                "input": payload.input,
                "artifact_ids": list(payload.artifact_ids),
                "attachments": attachments,
                "variables": payload.variables,
                "metadata": payload.metadata,
            },
            trace_id=trace_id,
            token_usage={},
        )
        await self.repo.create_execution(execution)
        await self.repo.add_message(
            conversation_id=conversation.id,
            role="user",
            content=user_text,
            execution_id=execution_id,
        )
        await self.db.commit()

        self._schedule(execution_id, actor, mode="start")
        return ExecutionOut.model_validate(execution)

    async def list_executions(
        self,
        actor: UserContext,
        *,
        agent_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ExecutionOut]:
        self._require(actor, "executions:read")
        if status is not None and status not in {item.value for item in ExecutionStatus}:
            raise ValidationError(f"Unknown execution status: {status}")
        rows = await self.repo.list_executions(
            user_id=None if actor.is_superuser else actor.user_id,
            agent_id=agent_id,
            status=status,
            limit=limit,
            offset=offset,
        )
        return [ExecutionOut.model_validate(row) for row in rows]

    async def get_execution(self, actor: UserContext, execution_id: str) -> ExecutionDetailOut:
        self._require(actor, "executions:read")
        execution = await self._load_execution(execution_id)
        await self._require_execution_read(actor, execution)
        approvals = await self.repo.list_approvals(execution_id=execution_id)
        llm_calls = await self.repo.list_llm_calls(execution_id)
        detail = ExecutionDetailOut.model_validate(execution)
        detail.events_count = await self.repo.count_events(execution_id)
        detail.approvals = [ApprovalOut.model_validate(row) for row in approvals]
        detail.llm_calls = [LLMCallOut.model_validate(row) for row in llm_calls]
        detail.input_payload = execution.input_payload or {}
        detail.output_payload = execution.output_payload
        return detail

    async def list_events(
        self,
        actor: UserContext,
        execution_id: str,
        *,
        after_seq: int = 0,
        limit: int = 500,
    ) -> list[ExecutionEventOut]:
        self._require(actor, "executions:read")
        execution = await self._load_execution(execution_id)
        await self._require_execution_read(actor, execution)
        rows = await self.repo.list_events(execution_id, after_seq=after_seq, limit=limit)
        return [ExecutionEventOut.model_validate(row) for row in rows]

    # --------------------------------------------------------------- cancel/resume

    async def cancel(self, actor: UserContext, execution_id: str) -> ExecutionOut:
        self._require(actor, "executions:write")
        execution = await self._load_execution(execution_id)
        await self._require_execution_write(actor, execution)
        if execution.status in {status.value for status in TERMINAL_EXECUTION_STATUSES}:
            raise ConflictError(f"Execution is already {execution.status}")

        now = _now()
        for approval in await self.repo.list_pending_approvals(execution_id):
            approval.status = "rejected"
            approval.resolved_at = now
            approval.resolved_by = actor.user_id
            approval.comment = "execution cancelled"
        execution.status = ExecutionStatus.CANCELLED.value
        execution.finished_at = now
        await self.db.commit()

        await ExecutionEventSink(self.session_factory, execution_id, broker=self.broker).emit(
            "execution_cancelled", {"reason": "cancelled by user", "by": actor.user_id}
        )
        task = _TASKS.get(execution_id)
        if task is not None and not task.done():
            task.cancel()
        return ExecutionOut.model_validate(execution)

    async def resume(self, actor: UserContext, execution_id: str) -> ExecutionOut:
        self._require(actor, "executions:write")
        execution = await self._load_execution(execution_id)
        await self._require_execution_write(actor, execution)
        if execution.status in {
            ExecutionStatus.COMPLETED.value,
            ExecutionStatus.CANCELLED.value,
            ExecutionStatus.RUNNING.value,
        }:
            raise ConflictError(f"Cannot resume an execution in status {execution.status}")

        pending = await self.repo.list_pending_approvals(execution_id)
        if pending:
            raise ConflictError("Resolve all pending approvals before resuming")

        decision: dict[str, Any] | None = None
        interrupt_id: str | None = None
        if execution.status == ExecutionStatus.WAITING_APPROVAL.value:
            resolved = await self.repo.list_resolved_approvals(execution_id)
            if resolved:
                # The runtime re-binds the interrupt id of whichever approval is
                # currently paused; prefer that one when resuming manually.
                bound = [
                    row for row in resolved if (row.payload or {}).get("interrupt_id")
                ]
                if bound:
                    approval = max(
                        bound,
                        key=lambda row: (row.payload or {}).get("interrupt_bound_at") or "",
                    )
                else:
                    approval = resolved[0]
                decision = {
                    "approved": approval.status == "approved",
                    "comment": approval.comment or "",
                }
                interrupt_id = (approval.payload or {}).get("interrupt_id")

        mode = "start" if execution.status == ExecutionStatus.PENDING.value else "resume"
        execution.status = ExecutionStatus.RUNNING.value
        execution.error = None
        if execution.started_at is None:
            execution.started_at = _now()
        await self.db.commit()

        owner = await self._owner_context(execution.user_id, fallback=actor)
        self._schedule(
            execution_id, owner, mode=mode, decision=decision, interrupt_id=interrupt_id
        )
        return ExecutionOut.model_validate(execution)

    # ----------------------------------------------------------------- approvals

    async def list_approvals(
        self,
        actor: UserContext,
        *,
        status: str | None = None,
        execution_id: str | None = None,
        limit: int = 200,
    ) -> list[ApprovalOut]:
        self._require(actor, "approvals:read")
        rows = await self.repo.list_approvals(
            execution_id=execution_id,
            status=status,
            owner_id=None if actor.is_superuser else actor.user_id,
            limit=limit,
        )
        return [ApprovalOut.model_validate(row) for row in rows]

    async def resolve_approval(
        self,
        actor: UserContext,
        approval_id: str,
        *,
        approved: bool,
        comment: str = "",
    ) -> ApprovalOut:
        self._require(actor, "approvals:write")
        approval = await self.repo.get_approval(approval_id)
        if approval is None:
            raise NotFoundError(f"Approval {approval_id} not found")
        if approval.status != "pending":
            raise ConflictError(f"Approval is already {approval.status}")

        execution = await self._load_execution(approval.execution_id)
        if execution.user_id == actor.user_id and not actor.is_superuser:
            raise PermissionDeniedError(
                "Self-approval is not allowed; another approver must decide"
            )
        if not (actor.is_superuser or execution.user_id == actor.user_id):
            await PermissionService(self.db).require_resource_access(
                actor,
                resource_type="execution",
                resource_id=execution.id,
                action="write",
                owner_id=execution.user_id,
            )

        approval.status = "approved" if approved else "rejected"
        approval.resolved_at = _now()
        approval.resolved_by = actor.user_id
        approval.comment = comment or None
        await self.db.commit()

        remaining = await self.repo.list_pending_approvals(execution.id)
        sink = ExecutionEventSink(self.session_factory, execution.id, broker=self.broker)
        interrupt_id = (approval.payload or {}).get("interrupt_id")
        if not remaining and execution.status == ExecutionStatus.WAITING_APPROVAL.value:
            owner = await self._owner_context(execution.user_id, fallback=actor)
            self._schedule(
                execution.id,
                owner,
                mode="resume",
                decision={"approved": approved, "comment": comment},
                interrupt_id=interrupt_id,
            )
        else:
            await sink.emit(
                "approval_resolved",
                {
                    "approval_id": approval.id,
                    "tool_name": approval.tool_name,
                    "approved": approved,
                    "comment": comment,
                    "by": actor.user_id,
                },
            )
        return ApprovalOut.model_validate(approval)

    async def _owner_context(self, user_id: str, *, fallback: UserContext) -> UserContext:
        try:
            return await PermissionService(self.db).load_context(user_id)
        except Exception:  # noqa: BLE001 - the owner may have been deleted
            return fallback

    # ------------------------------------------------------------------ recovery

    async def recover_stale_executions(
        self, *, reason: str = "interrupted by restart; resumable"
    ) -> int:
        rows = await self.repo.list_running_executions()
        if not rows:
            return 0
        now = _now()
        for row in rows:
            row.status = ExecutionStatus.FAILED.value
            row.error = reason
            row.finished_at = now
        await self.db.commit()
        for row in rows:
            await ExecutionEventSink(self.session_factory, row.id, broker=self.broker).emit(
                "execution_failed", {"error": reason, "recoverable": True}
            )
        return len(rows)

    # --------------------------------------------------------------- background

    def _schedule(
        self,
        execution_id: str,
        actor: UserContext,
        *,
        mode: str,
        decision: dict[str, Any] | None = None,
        interrupt_id: str | None = None,
    ) -> None:
        if not get_settings().worker_inline:
            return
        running = _TASKS.get(execution_id)
        if running is not None and not running.done():
            return
        task = asyncio.create_task(
            self._execute_task(
                execution_id, actor, mode=mode, decision=decision, interrupt_id=interrupt_id
            )
        )
        _TASKS[execution_id] = task

        def _cleanup(done: asyncio.Task[Any]) -> None:
            if _TASKS.get(execution_id) is done:
                _TASKS.pop(execution_id, None)

        task.add_done_callback(_cleanup)

    async def _execute_task(
        self,
        execution_id: str,
        actor: UserContext,
        *,
        mode: str,
        decision: dict[str, Any] | None,
        interrupt_id: str | None,
    ) -> None:
        async with self.session_factory() as db:
            worker = ExecutionService(
                db,
                agent_loader=self._agent_loader,
                tool_gateway=self._tool_gateway,
                model_gateway=self._model_gateway,
                skill_gateway=self._skill_gateway,
                knowledge_gateway=self._knowledge_gateway,
                broker=self.broker,
                session_factory=self.session_factory,
                checkpointer=self._checkpointer,
            )
            await worker._execute(
                execution_id, actor, mode=mode, decision=decision, interrupt_id=interrupt_id
            )

    async def _execute(
        self,
        execution_id: str,
        actor: UserContext,
        *,
        mode: str,
        decision: dict[str, Any] | None = None,
        interrupt_id: str | None = None,
    ) -> None:
        execution = await self.repo.get_execution(execution_id)
        if execution is None:
            return
        if execution.status in {
            ExecutionStatus.COMPLETED.value,
            ExecutionStatus.CANCELLED.value,
        }:
            return
        if mode == "start" and execution.status != ExecutionStatus.PENDING.value:
            return

        execution.status = ExecutionStatus.RUNNING.value
        execution.error = None
        if execution.started_at is None:
            execution.started_at = _now()
        await self.db.commit()

        sink = ExecutionEventSink(self.session_factory, execution_id, broker=self.broker)
        try:
            if mode == "start":
                await sink.emit(
                    "execution_started",
                    {
                        "agent_id": execution.agent_id,
                        "agent_version": execution.agent_version,
                        "conversation_id": execution.conversation_id,
                        "input": execution.input_text,
                        "trace_id": execution.trace_id,
                    },
                )
            async with contextlib.AsyncExitStack() as stack:
                (
                    model_gateway,
                    tool_gateway,
                    loader,
                    skill_gateway,
                    knowledge_gateway,
                ) = await self._gateways(stack)
                snapshot = await loader.load_published(execution.agent_id)
                history = await self._history(execution)
                runtime = RuntimeService(
                    model_gateway=model_gateway,
                    tool_gateway=tool_gateway,
                    sink=sink,
                    skill_gateway=skill_gateway,
                    knowledge_gateway=knowledge_gateway,
                    checkpointer=self._checkpointer,
                )
                if mode == "start":
                    result = await runtime.run(
                        execution_id=execution_id,
                        snapshot=snapshot,
                        user=actor,
                        history=history,
                        user_text=execution.input_text,
                        variables=(execution.input_payload or {}).get("variables") or {},
                    )
                else:
                    result = await runtime.resume(
                        execution_id=execution_id,
                        user=actor,
                        snapshot=snapshot,
                        decision=decision,
                        interrupt_id=interrupt_id,
                    )
            await self._apply_result(execution_id, result, sink)
        except asyncio.CancelledError:
            await self._ensure_cancelled(execution_id)
            raise
        except Exception as exc:  # noqa: BLE001 - any crash marks the execution failed
            logger.exception("execution crashed", extra={"execution_id": execution_id})
            await self._ensure_failed(execution_id, str(exc), sink)

    async def _gateways(
        self, stack: contextlib.AsyncExitStack
    ) -> tuple[ModelGateway, ToolGateway, AgentLoader, SkillGateway | None, KnowledgeGateway | None]:
        if (
            self._model_gateway
            and self._tool_gateway
            and self._agent_loader
            and self._skill_gateway
            and self._knowledge_gateway
        ):
            return (
                self._model_gateway,
                self._tool_gateway,
                self._agent_loader,
                self._skill_gateway,
                self._knowledge_gateway,
            )
        db = await stack.enter_async_context(self.session_factory())
        model_gateway = self._model_gateway or _model_service(db)
        tool_gateway = self._tool_gateway or _tool_service(db)
        loader = self._agent_loader or _agent_service(db)
        if self._skill_gateway is None:
            from app.modules.skill.service import SkillService

            skill_gateway = SkillService(db)
        else:
            skill_gateway = self._skill_gateway
        if self._knowledge_gateway is None:
            from app.modules.knowledge.service import KnowledgeService

            knowledge_gateway = KnowledgeService(db)
        else:
            knowledge_gateway = self._knowledge_gateway
        return model_gateway, tool_gateway, loader, skill_gateway, knowledge_gateway

    async def _history(self, execution: AgentExecution) -> list[ChatMessage]:
        if not execution.conversation_id:
            return []
        rows = await self.repo.list_messages(
            execution.conversation_id, before_execution_id=execution.id
        )
        history: list[ChatMessage] = []
        for row in rows:
            if row.role == "tool":
                continue
            history.append(
                ChatMessage(
                    role=row.role,  # type: ignore[arg-type]
                    content=row.content,
                    tool_calls=list(row.tool_calls or []),
                )
            )
        return history

    async def _apply_result(
        self, execution_id: str, result: RuntimeResult, sink: ExecutionEventSink
    ) -> None:
        execution = await self.repo.get_execution(execution_id)
        if execution is None:
            return
        await self.db.refresh(execution)
        if execution.status == ExecutionStatus.CANCELLED.value:
            return

        execution.token_usage = dict(result.usage or {})
        execution.output_payload = dict(result.output_payload or {})
        if result.output_text is not None:
            execution.output_text = result.output_text

        if result.status == "waiting_approval":
            execution.status = ExecutionStatus.WAITING_APPROVAL.value
            await self.db.commit()
            return

        if result.status == "completed":
            execution.status = ExecutionStatus.COMPLETED.value
            execution.finished_at = _now()
            if execution.conversation_id:
                await self.repo.add_message(
                    conversation_id=execution.conversation_id,
                    role="assistant",
                    content=result.output_text or "",
                    tool_calls=(result.output_payload or {}).get("tool_calls") or None,
                    execution_id=execution.id,
                )
            await self.db.commit()
            await sink.emit(
                "execution_completed",
                {
                    "status": execution.status,
                    "output": result.output_text,
                    "usage": result.usage,
                    "agent_id": execution.agent_id,
                    "agent_version": execution.agent_version,
                },
            )
            return

        execution.status = ExecutionStatus.FAILED.value
        execution.error = result.error or "execution failed"
        execution.finished_at = _now()
        await self.db.commit()
        await sink.emit(
            "execution_failed",
            {
                "error": execution.error,
                "agent_id": execution.agent_id,
                "agent_version": execution.agent_version,
            },
        )

    async def _ensure_failed(
        self, execution_id: str, error: str, sink: ExecutionEventSink | None = None
    ) -> None:
        try:
            execution = await self.repo.get_execution(execution_id)
            if execution is None:
                return
            await self.db.refresh(execution)
            if execution.status in {
                ExecutionStatus.COMPLETED.value,
                ExecutionStatus.FAILED.value,
                ExecutionStatus.CANCELLED.value,
            }:
                return
            execution.status = ExecutionStatus.FAILED.value
            execution.error = error
            execution.finished_at = _now()
            await self.db.commit()
        except Exception:  # noqa: BLE001 - never mask the original failure
            await self.db.rollback()
            return
        sink = sink or ExecutionEventSink(self.session_factory, execution_id, broker=self.broker)
        await sink.emit("execution_failed", {"error": error})

    async def _ensure_cancelled(self, execution_id: str) -> None:
        try:
            execution = await self.repo.get_execution(execution_id)
            if execution is None:
                return
            await self.db.refresh(execution)
            if execution.status == ExecutionStatus.CANCELLED.value:
                return
            if execution.status in {
                ExecutionStatus.COMPLETED.value,
                ExecutionStatus.FAILED.value,
            }:
                return
            execution.status = ExecutionStatus.CANCELLED.value
            execution.finished_at = _now()
            await self.db.commit()
        except Exception:  # noqa: BLE001
            await self.db.rollback()
            return
        await ExecutionEventSink(self.session_factory, execution_id, broker=self.broker).emit(
            "execution_cancelled", {"reason": "task cancelled"}
        )

    # -------------------------------------------------------------------- test

    async def wait_for_terminal(self, execution_id: str, *, timeout: float = 30.0) -> str:
        """Poll until the execution reaches a terminal status (used by tests/workers)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        terminal = {status.value for status in TERMINAL_EXECUTION_STATUSES}
        while loop.time() < deadline:
            async with self.session_factory() as db:
                execution = await db.get(AgentExecution, execution_id)
                if execution is not None and execution.status in terminal:
                    return execution.status
            await asyncio.sleep(0.05)
        raise ExecutionError(f"execution {execution_id} did not finish within {timeout}s")


__all__ = ["ExecutionService"]
