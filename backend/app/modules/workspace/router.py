from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import require_permission
from app.core.contracts import UserContext
from app.infrastructure.database.session import get_db
from app.modules.execution.schemas import ConversationOut, ExecutionOut, MessageOut, TaskOut
from app.modules.workspace.schemas import (
    FileRootAccessOut,
    FileRootCreate,
    FileRootEntryOut,
    FileRootGrantCreate,
    FileRootGrantOut,
    FileRootOut,
    UsableAgentOut,
    WorkspaceConversationCreate,
)
from app.modules.workspace.service import WorkspaceService

router = APIRouter(prefix="/workspaces", tags=["workspaces"])
file_roots_router = APIRouter(tags=["file-roots"])


def get_workspace_service(db: AsyncSession = Depends(get_db)) -> WorkspaceService:
    return WorkspaceService(db)


@router.get("/agents", response_model=list[UsableAgentOut])
async def list_workspace_agents(
    user: UserContext = Depends(require_permission("executions:write")),
    service: WorkspaceService = Depends(get_workspace_service),
):
    return await service.list_usable_agents(user)


@router.get(
    "/agents/{agent_id}/conversations", response_model=list[ConversationOut]
)
async def list_workspace_conversations(
    agent_id: str,
    limit: int = Query(100, ge=1, le=500),
    user: UserContext = Depends(require_permission("executions:write")),
    service: WorkspaceService = Depends(get_workspace_service),
):
    return await service.list_conversations(user, agent_id, limit=limit)


@router.post(
    "/agents/{agent_id}/conversations",
    response_model=ConversationOut,
    status_code=201,
)
async def create_workspace_conversation(
    agent_id: str,
    payload: WorkspaceConversationCreate | None = None,
    user: UserContext = Depends(require_permission("executions:write")),
    service: WorkspaceService = Depends(get_workspace_service),
):
    return await service.create_conversation(
        user,
        agent_id,
        title=(payload.title if payload else ""),
        root_id=(payload.root_id if payload else None),
    )


@router.get(
    "/agents/{agent_id}/conversations/{conversation_id}",
    response_model=ConversationOut,
)
async def get_workspace_conversation(
    agent_id: str,
    conversation_id: str,
    user: UserContext = Depends(require_permission("executions:write")),
    service: WorkspaceService = Depends(get_workspace_service),
):
    return await service.get_conversation(user, agent_id, conversation_id)


@router.get(
    "/agents/{agent_id}/conversations/{conversation_id}/messages",
    response_model=list[MessageOut],
)
async def list_workspace_messages(
    agent_id: str,
    conversation_id: str,
    limit: int = Query(500, ge=1, le=2000),
    user: UserContext = Depends(require_permission("executions:write")),
    service: WorkspaceService = Depends(get_workspace_service),
):
    return await service.list_messages(
        user, agent_id, conversation_id, limit=limit
    )


@router.get("/agents/{agent_id}/executions", response_model=list[ExecutionOut])
async def list_workspace_executions(
    agent_id: str,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_permission("executions:write")),
    service: WorkspaceService = Depends(get_workspace_service),
):
    return await service.list_executions(user, agent_id, limit=limit, offset=offset)


@router.get("/agents/{agent_id}/tasks", response_model=list[TaskOut])
async def list_workspace_tasks(
    agent_id: str,
    limit: int = Query(100, ge=1, le=200),
    user: UserContext = Depends(require_permission("executions:write")),
    service: WorkspaceService = Depends(get_workspace_service),
):
    return await service.list_tasks(user, agent_id, limit=limit)


@router.get(
    "/agents/{agent_id}/file-roots", response_model=list[FileRootAccessOut]
)
async def list_workspace_file_roots(
    agent_id: str,
    user: UserContext = Depends(require_permission("executions:write")),
    service: WorkspaceService = Depends(get_workspace_service),
):
    return await service.list_agent_roots(user, agent_id)


@router.get(
    "/agents/{agent_id}/file-roots/{root_id}/entries",
    response_model=list[FileRootEntryOut],
)
async def list_workspace_file_entries(
    agent_id: str,
    root_id: str,
    path: str = Query(default=""),
    user: UserContext = Depends(require_permission("executions:write")),
    service: WorkspaceService = Depends(get_workspace_service),
):
    return await service.list_entries(user, agent_id, root_id, path=path)


@router.get("/agents/{agent_id}/file-roots/{root_id}/content")
async def download_workspace_file(
    agent_id: str,
    root_id: str,
    path: str = Query(default=""),
    user: UserContext = Depends(require_permission("executions:write")),
    service: WorkspaceService = Depends(get_workspace_service),
):
    target = await service.file_path(user, agent_id, root_id, path=path)
    return FileResponse(target, filename=target.name)


@router.post(
    "/agents/{agent_id}/file-roots/{root_id}/files",
    response_model=FileRootEntryOut,
    status_code=201,
)
async def upload_workspace_file(
    agent_id: str,
    root_id: str,
    file: UploadFile = File(...),
    path: str = Form(...),
    user: UserContext = Depends(require_permission("executions:write")),
    service: WorkspaceService = Depends(get_workspace_service),
):
    return await service.upload(user, agent_id, root_id, file, path=path)


@router.delete(
    "/agents/{agent_id}/file-roots/{root_id}/files",
    status_code=204,
)
async def delete_workspace_file(
    agent_id: str,
    root_id: str,
    path: str = Query(...),
    user: UserContext = Depends(require_permission("executions:write")),
    service: WorkspaceService = Depends(get_workspace_service),
):
    await service.delete_file(user, agent_id, root_id, path=path)


# --------------------------------------------------------------- admin: roots


@file_roots_router.post("/file-roots", response_model=FileRootOut, status_code=201)
async def create_file_root(
    payload: FileRootCreate,
    user: UserContext = Depends(require_permission("agents:write")),
    service: WorkspaceService = Depends(get_workspace_service),
):
    return await service.create_root(user, payload)


@file_roots_router.get("/file-roots", response_model=list[FileRootOut])
async def list_file_roots(
    user: UserContext = Depends(require_permission("agents:write")),
    service: WorkspaceService = Depends(get_workspace_service),
):
    return await service.list_roots(user)


@file_roots_router.post(
    "/file-roots/{root_id}/permissions",
    response_model=FileRootGrantOut,
    status_code=201,
)
async def grant_file_root_permission(
    root_id: str,
    payload: FileRootGrantCreate,
    user: UserContext = Depends(require_permission("agents:write")),
    service: WorkspaceService = Depends(get_workspace_service),
):
    return await service.grant_agent(user, root_id, payload)


@file_roots_router.get(
    "/file-roots/{root_id}/permissions",
    response_model=list[FileRootGrantOut],
)
async def list_file_root_permissions(
    root_id: str,
    user: UserContext = Depends(require_permission("agents:write")),
    service: WorkspaceService = Depends(get_workspace_service),
):
    return await service.list_root_grants(user, root_id)


@file_roots_router.delete(
    "/file-roots/{root_id}/permissions/{agent_id}",
    status_code=204,
)
async def revoke_file_root_permission(
    root_id: str,
    agent_id: str,
    user: UserContext = Depends(require_permission("agents:write")),
    service: WorkspaceService = Depends(get_workspace_service),
):
    await service.revoke_agent(user, root_id, agent_id)


__all__ = ["file_roots_router", "get_workspace_service", "router"]
