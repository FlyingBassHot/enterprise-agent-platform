from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.contracts import UserContext
from app.core.exceptions import NotFoundError, PermissionDeniedError, ValidationError
from app.modules.audit_log.models import AuditLog
from app.modules.audit_log.repository import AuditLogRepository
from app.modules.audit_log.schemas import AuditLogOut

READ_PERMISSION = "audit:logs:read"
MAX_LIMIT = 500
MAX_DETAILS_CHARS = 4000
RESULT_VALUES = frozenset({"success", "failure", "denied"})
_SECRET_MARKERS = ("password", "token", "secret", "api_key", "authorization", "key")
_REDACTED = "***"


def _normalize_key(key: object) -> str:
    return str(key).lower().replace("-", "_").replace(" ", "_")


def _is_secret_key(key: object) -> bool:
    normalized = _normalize_key(key)
    return any(marker in normalized for marker in _SECRET_MARKERS)


def _mask(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _REDACTED if _is_secret_key(key) else _mask(item) for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [_mask(item) for item in value]
    return value


def _serialize(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)


def _sanitize_details(details: Any) -> dict[str, Any]:
    """Recursively redact secret-looking keys and bound the stored JSON size."""
    if details is None:
        return {}
    masked = _mask(details)
    if not isinstance(masked, dict):
        masked = {"value": masked}
    if len(_serialize(masked)) <= MAX_DETAILS_CHARS:
        return masked

    trimmed: dict[str, Any] = {}
    for key in sorted(masked, key=str):
        candidate = {**trimmed, key: masked[key]}
        # Leave headroom for the trailing marker so the result still fits.
        if len(_serialize(candidate)) > MAX_DETAILS_CHARS - 64:
            break
        trimmed[key] = masked[key]
    trimmed["_truncated"] = True
    return trimmed


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class AuditService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = AuditLogRepository(db)

    async def record(
        self,
        *,
        actor_id: str | None = None,
        actor_username: str = "",
        action: str,
        resource_type: str = "",
        resource_id: str | None = None,
        result: str = "success",
        status_code: int | None = None,
        ip_address: str | None = None,
        request_id: str | None = None,
        tenant_id: str = "default",
        details: dict[str, Any] | None = None,
        commit: bool = True,
    ) -> AuditLogOut:
        """Append one audit entry.

        ``commit=False`` flushes but does not commit, so the caller can write
        the audit row inside an existing transaction (and roll it back with
        the business change).
        """
        if not action or not action.strip():
            raise ValidationError("Audit action is required")
        if result not in RESULT_VALUES:
            raise ValidationError(f"Invalid audit result: {result}")

        row = AuditLog(
            occurred_at=datetime.now(UTC),
            actor_id=actor_id,
            actor_username=actor_username or "",
            action=action.strip(),
            resource_type=resource_type or "",
            resource_id=resource_id,
            result=result,
            status_code=status_code,
            ip_address=ip_address,
            request_id=request_id,
            tenant_id=tenant_id or "default",
            details=_sanitize_details(details),
        )
        self.repo.add(row)
        await self.db.flush()
        if commit:
            await self.db.commit()
        return AuditLogOut.model_validate(row)

    async def list_logs(
        self,
        actor: UserContext,
        *,
        action: str | None = None,
        actor_id: str | None = None,
        resource_type: str | None = None,
        result: str | None = None,
        after: datetime | None = None,
        before: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AuditLogOut]:
        if not actor.has(READ_PERMISSION):
            raise PermissionDeniedError(f"Missing permission: {READ_PERMISSION}")
        limit = min(max(int(limit), 1), MAX_LIMIT)
        offset = max(int(offset), 0)
        rows = await self.repo.list_logs(
            action=action,
            actor_id=actor_id,
            resource_type=resource_type,
            result=result,
            after=_ensure_utc(after) if after is not None else None,
            before=_ensure_utc(before) if before is not None else None,
            limit=limit,
            offset=offset,
        )
        return [AuditLogOut.model_validate(row) for row in rows]

    async def get_log(self, actor: UserContext, log_id: str) -> AuditLogOut:
        if not actor.has(READ_PERMISSION):
            raise PermissionDeniedError(f"Missing permission: {READ_PERMISSION}")
        row = await self.repo.get(log_id)
        if row is None:
            raise NotFoundError("Audit log not found")
        return AuditLogOut.model_validate(row)
