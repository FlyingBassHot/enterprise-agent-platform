from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import require_permission
from app.core.contracts import UserContext
from app.infrastructure.database.session import get_db
from app.modules.execution.schemas import ExecutionCreate, ExecutionOut
from app.modules.execution.service import ExecutionService
from app.modules.workflow.schemas import WorkflowExecuteRequest

router = APIRouter(tags=["workflows"])


@router.post(
    "/workflows/{workflow_id}/execute",
    response_model=ExecutionOut,
    status_code=201,
)
async def execute_workflow(
    workflow_id: str,
    payload: WorkflowExecuteRequest,
    actor: UserContext = Depends(
        require_permission("executions:write", "workflows:read")
    ),
    db: AsyncSession = Depends(get_db),
):
    return await ExecutionService(db).create_execution(
        actor,
        ExecutionCreate(
            workflow_id=workflow_id,
            input=payload.input,
            variables=payload.variables,
            metadata=payload.metadata,
        ),
    )
