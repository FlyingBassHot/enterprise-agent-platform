from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database.base import Base, TimestampMixin, UUIDMixin


class Tool(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "tools"

    name: Mapped[str] = mapped_column(String(120), unique=True, index=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    version: Mapped[str] = mapped_column(String(40), default="1", nullable=False)
    source_type: Mapped[str] = mapped_column(String(16), default="native", nullable=False)
    input_schema: Mapped[dict] = mapped_column(default=dict, nullable=False)
    output_schema: Mapped[dict] = mapped_column(default=dict, nullable=False)
    risk_level: Mapped[str] = mapped_column(String(16), default="low", nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    permission_scope: Mapped[list[str]] = mapped_column(default=list, nullable=False)
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    config: Mapped[dict] = mapped_column(default=dict, nullable=False)
    sensitive_fields: Mapped[list[str]] = mapped_column(default=list, nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)


class MCPServer(UUIDMixin, TimestampMixin, Base):
    """Admin-managed MCP server registration.

    ``env`` and ``headers`` store templates only: values must be literals or
    ``${TOOL_SECRET_*}`` references, never resolved secrets.
    """

    __tablename__ = "mcp_servers"

    name: Mapped[str] = mapped_column(String(120), unique=True, index=True, nullable=False)
    transport: Mapped[str] = mapped_column(String(8), default="stdio", nullable=False)
    command: Mapped[str | None] = mapped_column(String(500), nullable=True)
    args: Mapped[list[str]] = mapped_column(default=list, nullable=False)
    env: Mapped[dict[str, str]] = mapped_column(default=dict, nullable=False)
    url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    headers: Mapped[dict[str, str]] = mapped_column(default=dict, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)


class ToolVersion(UUIDMixin, Base):
    __tablename__ = "tool_versions"
    __table_args__ = (UniqueConstraint("tool_id", "version", name="uq_tool_version"),)

    tool_id: Mapped[str] = mapped_column(
        ForeignKey("tools.id", ondelete="CASCADE"), index=True, nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict] = mapped_column(default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ToolPermission(UUIDMixin, Base):
    __tablename__ = "tool_permissions"
    __table_args__ = (
        UniqueConstraint(
            "tool_id", "subject_type", "subject_id", name="uq_tool_permission_subject"
        ),
    )

    tool_id: Mapped[str] = mapped_column(
        ForeignKey("tools.id", ondelete="CASCADE"), index=True, nullable=False
    )
    subject_type: Mapped[str] = mapped_column(String(16), nullable=False)  # user | role
    subject_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    actions: Mapped[list[str]] = mapped_column(default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ToolExecution(UUIDMixin, Base):
    __tablename__ = "tool_executions"

    tool_id: Mapped[str | None] = mapped_column(
        ForeignKey("tools.id", ondelete="SET NULL"), index=True, nullable=True
    )
    tool_name: Mapped[str] = mapped_column(String(120), index=True, nullable=False)
    execution_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    arguments_masked: Mapped[dict] = mapped_column(default=dict, nullable=False)
    result: Mapped[Any] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="success", nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True, nullable=False
    )
