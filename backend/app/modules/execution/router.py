from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    Query,
    UploadFile,
)
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_user, require_permission
from app.core.config import get_settings
from app.core.contracts import UserContext
from app.core.enums import TERMINAL_EXECUTION_STATUSES
from app.infrastructure.database.session import get_db
from app.modules.execution.artifacts import ArtifactService
from app.modules.execution.events import (
    TERMINAL_EVENT_TYPES,
    format_sse,
    replay_events,
)
from app.modules.execution.models import AgentExecution
from app.modules.execution.schemas import (
    ApprovalDecision,
    ApprovalOut,
    ArtifactOut,
    ConversationCreate,
    ConversationOut,
    ExecutionCreate,
    ExecutionDetailOut,
    ExecutionEventOut,
    ExecutionOut,
    MessageOut,
    ResumeExecutionRequest,
)
from app.modules.execution.service import ExecutionService
from app.modules.tool.schemas import ToolExecutionOut

router = APIRouter(tags=["executions"])


def get_execution_service(db: AsyncSession = Depends(get_db)) -> ExecutionService:
    return ExecutionService(db)


def get_artifact_service(db: AsyncSession = Depends(get_db)) -> ArtifactService:
    return ArtifactService(db)


# ---------------------------------------------------------------------------
# Executions
# ---------------------------------------------------------------------------


@router.post("/executions", response_model=ExecutionOut, status_code=201)
async def create_execution(
    payload: ExecutionCreate,
    actor: UserContext = Depends(require_permission("executions:write")),
    service: ExecutionService = Depends(get_execution_service),
):
    return await service.create_execution(actor, payload)


@router.get("/executions", response_model=list[ExecutionOut])
async def list_executions(
    agent_id: str | None = None,
    status: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    actor: UserContext = Depends(require_permission("executions:read")),
    service: ExecutionService = Depends(get_execution_service),
):
    return await service.list_executions(
        actor, agent_id=agent_id, status=status, limit=limit, offset=offset
    )


@router.get("/executions/{execution_id}", response_model=ExecutionDetailOut)
async def get_execution(
    execution_id: str,
    actor: UserContext = Depends(require_permission("executions:read")),
    service: ExecutionService = Depends(get_execution_service),
):
    return await service.get_execution(actor, execution_id)


@router.get("/executions/{execution_id}/events", response_model=list[ExecutionEventOut])
async def list_execution_events(
    execution_id: str,
    after_seq: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=2000),
    actor: UserContext = Depends(require_permission("executions:read")),
    service: ExecutionService = Depends(get_execution_service),
):
    return await service.list_events(actor, execution_id, after_seq=after_seq, limit=limit)


@router.get(
    "/executions/{execution_id}/tool-calls", response_model=list[ToolExecutionOut]
)
async def list_execution_tool_calls(
    execution_id: str,
    actor: UserContext = Depends(require_permission("executions:read")),
    service: ExecutionService = Depends(get_execution_service),
):
    await service.get_execution(actor, execution_id)
    from app.modules.tool.service import ToolService

    return await ToolService(service.db).list_execution_records(execution_id)


@router.get("/executions/{execution_id}/stream")
async def stream_execution(
    execution_id: str,
    after: int = Query(0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    actor: UserContext = Depends(get_current_user),
    service: ExecutionService = Depends(get_execution_service),
):
    await service.get_execution(actor, execution_id)
    after_seq = after
    if last_event_id:
        try:
            after_seq = max(after_seq, int(last_event_id))
        except ValueError:
            pass
    broker = service.broker
    session_factory = service.session_factory
    keepalive = max(1, int(get_settings().sse_keepalive_seconds))
    terminal_statuses = {status.value for status in TERMINAL_EXECUTION_STATUSES}

    async def generator() -> AsyncIterator[str]:
        queue = broker.subscribe(execution_id)
        last = after_seq
        try:
            while True:
                batch = await replay_events(
                    session_factory, execution_id, after_seq=last, limit=2000
                )
                for event in batch:
                    last = max(last, int(event["seq"]))
                    yield format_sse(event)
                if len(batch) < 2000:
                    break
            async with session_factory() as db:
                row = await db.get(AgentExecution, execution_id)
                finished = row is not None and row.status in terminal_statuses
            if finished:
                while not queue.empty():
                    event = queue.get_nowait()
                    if int(event["seq"]) > last:
                        last = int(event["seq"])
                        yield format_sse(event)
                return
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=keepalive)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if int(event["seq"]) <= last:
                    continue
                last = int(event["seq"])
                yield format_sse(event)
                if event["type"] in TERMINAL_EVENT_TYPES:
                    return
        finally:
            broker.unsubscribe(execution_id, queue)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/executions/{execution_id}/cancel", response_model=ExecutionOut)
async def cancel_execution(
    execution_id: str,
    actor: UserContext = Depends(require_permission("executions:write")),
    service: ExecutionService = Depends(get_execution_service),
):
    return await service.cancel(actor, execution_id)


@router.post("/executions/{execution_id}/resume", response_model=ExecutionOut)
async def resume_execution(
    execution_id: str,
    payload: ResumeExecutionRequest | None = None,
    actor: UserContext = Depends(require_permission("executions:write")),
    service: ExecutionService = Depends(get_execution_service),
):
    return await service.resume(
        actor,
        execution_id,
        extra_iterations=payload.extra_iterations if payload else None,
    )


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------


@router.post("/conversations", response_model=ConversationOut, status_code=201)
async def create_conversation(
    payload: ConversationCreate,
    actor: UserContext = Depends(require_permission("executions:write")),
    service: ExecutionService = Depends(get_execution_service),
):
    return await service.create_conversation(actor, payload)


@router.get("/conversations", response_model=list[ConversationOut])
async def list_conversations(
    agent_id: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    actor: UserContext = Depends(require_permission("executions:read")),
    service: ExecutionService = Depends(get_execution_service),
):
    return await service.list_conversations(actor, agent_id=agent_id, limit=limit)


@router.get("/conversations/{conversation_id}", response_model=ConversationOut)
async def get_conversation(
    conversation_id: str,
    actor: UserContext = Depends(require_permission("executions:read")),
    service: ExecutionService = Depends(get_execution_service),
):
    return await service.get_conversation(actor, conversation_id)


@router.get("/conversations/{conversation_id}/messages", response_model=list[MessageOut])
async def list_messages(
    conversation_id: str,
    limit: int = Query(500, ge=1, le=2000),
    actor: UserContext = Depends(require_permission("executions:read")),
    service: ExecutionService = Depends(get_execution_service),
):
    return await service.list_messages(actor, conversation_id, limit=limit)


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------


@router.post("/artifacts", response_model=ArtifactOut, status_code=201)
async def upload_artifact(
    file: UploadFile = File(...),
    conversation_id: str | None = Form(default=None),
    actor: UserContext = Depends(require_permission("executions:write")),
    service: ArtifactService = Depends(get_artifact_service),
):
    return await service.save(actor, file, conversation_id=conversation_id)


@router.get("/artifacts/{artifact_id}/meta", response_model=ArtifactOut)
async def artifact_meta(
    artifact_id: str,
    actor: UserContext = Depends(get_current_user),
    service: ArtifactService = Depends(get_artifact_service),
):
    return await service.get_meta(actor, artifact_id)


@router.get("/artifacts/{artifact_id}")
async def download_artifact(
    artifact_id: str,
    actor: UserContext = Depends(get_current_user),
    service: ArtifactService = Depends(get_artifact_service),
):
    artifact, path = await service.file_path(actor, artifact_id)
    return FileResponse(
        path,
        media_type=artifact.content_type,
        filename=artifact.filename,
    )


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------


@router.get("/approvals", response_model=list[ApprovalOut])
async def list_approvals(
    status: str | None = None,
    execution_id: str | None = None,
    limit: int = Query(200, ge=1, le=500),
    actor: UserContext = Depends(require_permission("approvals:read")),
    service: ExecutionService = Depends(get_execution_service),
):
    return await service.list_approvals(
        actor, status=status, execution_id=execution_id, limit=limit
    )


@router.post("/approvals/{approval_id}/approve", response_model=ApprovalOut)
async def approve(
    approval_id: str,
    payload: ApprovalDecision | None = None,
    actor: UserContext = Depends(require_permission("approvals:write")),
    service: ExecutionService = Depends(get_execution_service),
):
    return await service.resolve_approval(
        actor, approval_id, approved=True, comment=(payload.comment if payload else "")
    )


@router.post("/approvals/{approval_id}/reject", response_model=ApprovalOut)
async def reject(
    approval_id: str,
    payload: ApprovalDecision | None = None,
    actor: UserContext = Depends(require_permission("approvals:write")),
    service: ExecutionService = Depends(get_execution_service),
):
    return await service.resolve_approval(
        actor, approval_id, approved=False, comment=(payload.comment if payload else "")
    )


__all__ = ["get_artifact_service", "get_execution_service", "router"]
