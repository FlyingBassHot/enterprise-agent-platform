from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit_log.models import AuditLog


class AuditLogRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    def add(self, row: AuditLog) -> None:
        self.db.add(row)

    async def get(self, log_id: str) -> AuditLog | None:
        return await self.db.get(AuditLog, log_id)

    async def list_logs(
        self,
        *,
        action: str | None = None,
        actor_id: str | None = None,
        resource_type: str | None = None,
        result: str | None = None,
        after: datetime | None = None,
        before: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AuditLog]:
        stmt = select(AuditLog)
        if action is not None:
            stmt = stmt.where(AuditLog.action == action)
        if actor_id is not None:
            stmt = stmt.where(AuditLog.actor_id == actor_id)
        if resource_type is not None:
            stmt = stmt.where(AuditLog.resource_type == resource_type)
        if result is not None:
            stmt = stmt.where(AuditLog.result == result)
        if after is not None:
            stmt = stmt.where(AuditLog.occurred_at >= after)
        if before is not None:
            stmt = stmt.where(AuditLog.occurred_at <= before)
        stmt = (
            stmt.order_by(AuditLog.occurred_at.desc(), AuditLog.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list((await self.db.scalars(stmt)).all())
