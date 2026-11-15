from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import require_permission
from app.core.contracts import UserContext
from app.infrastructure.database.session import get_db
from app.modules.audit_log.schemas import AuditLogOut
from app.modules.audit_log.service import MAX_LIMIT, READ_PERMISSION, AuditService

router = APIRouter(tags=["audit"])


@router.get("/audit-logs", response_model=list[AuditLogOut])
async def list_audit_logs(
    action: str | None = Query(default=None),
    actor_id: str | None = Query(default=None),
    resource_type: str | None = Query(default=None),
    result: str | None = Query(default=None, pattern="^(success|failure|denied)$"),
    after: datetime | None = Query(default=None),
    before: datetime | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    actor: UserContext = Depends(require_permission(READ_PERMISSION)),
    db: AsyncSession = Depends(get_db),
):
    return await AuditService(db).list_logs(
        actor,
        action=action,
        actor_id=actor_id,
        resource_type=resource_type,
        result=result,
        after=after,
        before=before,
        limit=limit,
        offset=offset,
    )


@router.get("/audit-logs/{log_id}", response_model=AuditLogOut)
async def get_audit_log(
    log_id: str,
    actor: UserContext = Depends(require_permission(READ_PERMISSION)),
    db: AsyncSession = Depends(get_db),
):
    return await AuditService(db).get_log(actor, log_id)
