from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database.base import Base, UUIDMixin


class AuditLog(UUIDMixin, Base):
    """Append-only audit trail entry.

    There is intentionally no ``updated_at``: rows are written once and never
    mutated through the application. ``created_at`` is the persistence
    timestamp, ``occurred_at`` the time of the audited event (they can differ
    when events are buffered and flushed later).
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_occurred_at_action_actor_id", "occurred_at", "action", "actor_id"),
    )

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        index=True,
        nullable=False,
    )
    actor_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    actor_username: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    action: Mapped[str] = mapped_column(String(200), index=True, nullable=False)
    resource_type: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # success | failure | denied
    result: Mapped[str] = mapped_column(String(16), default="success", nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tenant_id: Mapped[str] = mapped_column(String(64), default="default", nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        nullable=False,
    )
