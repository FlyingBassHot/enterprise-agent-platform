from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import require_permission
from app.core.contracts import UserContext
from app.infrastructure.database.session import get_db
from app.modules.workflow.schemas import (
    WorkflowCreate,
    WorkflowOut,
    WorkflowUpdate,
    WorkflowValidationResult,
    WorkflowVersionOut,
)
from app.modules.workflow.service import WorkflowService

router = APIRouter(tags=["workflows"])


@router.get("/workflows", response_model=list[WorkflowOut])
async def list_workflows(
    actor: UserContext = Depends(require_permission("workflows:read")),
    db: AsyncSession = Depends(get_db),
):
    return await WorkflowService(db).list_workflows(actor)


@router.post("/workflows", response_model=WorkflowOut, status_code=201)
async def create_workflow(
    payload: WorkflowCreate,
    actor: UserContext = Depends(require_permission("workflows:write")),
    db: AsyncSession = Depends(get_db),
):
    return await WorkflowService(db).create_workflow(actor, payload)


@router.get("/workflows/{workflow_id}", response_model=WorkflowOut)
async def get_workflow(
    workflow_id: str,
    actor: UserContext = Depends(require_permission("workflows:read")),
    db: AsyncSession = Depends(get_db),
):
    return await WorkflowService(db).get_workflow(actor, workflow_id)


@router.put("/workflows/{workflow_id}", response_model=WorkflowOut)
async def update_workflow(
    workflow_id: str,
    payload: WorkflowUpdate,
    actor: UserContext = Depends(require_permission("workflows:write")),
    db: AsyncSession = Depends(get_db),
):
    return await WorkflowService(db).update_workflow(actor, workflow_id, payload)


@router.delete("/workflows/{workflow_id}", status_code=204)
async def delete_workflow(
    workflow_id: str,
    actor: UserContext = Depends(require_permission("workflows:write")),
    db: AsyncSession = Depends(get_db),
):
    await WorkflowService(db).delete_workflow(actor, workflow_id)


@router.post(
    "/workflows/{workflow_id}/validate", response_model=WorkflowValidationResult
)
async def validate_workflow(
    workflow_id: str,
    actor: UserContext = Depends(require_permission("workflows:read")),
    db: AsyncSession = Depends(get_db),
):
    return await WorkflowService(db).validate(actor, workflow_id)


@router.post("/workflows/{workflow_id}/publish", response_model=WorkflowOut)
async def publish_workflow(
    workflow_id: str,
    actor: UserContext = Depends(require_permission("workflows:publish")),
    db: AsyncSession = Depends(get_db),
):
    return await WorkflowService(db).publish(actor, workflow_id)


@router.post("/workflows/{workflow_id}/disable", response_model=WorkflowOut)
async def disable_workflow(
    workflow_id: str,
    actor: UserContext = Depends(require_permission("workflows:publish")),
    db: AsyncSession = Depends(get_db),
):
    return await WorkflowService(db).disable(actor, workflow_id)


@router.get(
    "/workflows/{workflow_id}/versions", response_model=list[WorkflowVersionOut]
)
async def list_workflow_versions(
    workflow_id: str,
    actor: UserContext = Depends(require_permission("workflows:read")),
    db: AsyncSession = Depends(get_db),
):
    return await WorkflowService(db).list_versions(actor, workflow_id)
