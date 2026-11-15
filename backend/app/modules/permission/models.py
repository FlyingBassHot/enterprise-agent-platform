from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database.base import Base, TimestampMixin, UUIDMixin


class User(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "users"

    username: Mapped[str] = mapped_column(String(80), unique=True, index=True, nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    display_name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_superuser: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    department_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tenant_id: Mapped[str] = mapped_column(String(64), default="default", nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Role(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "roles"

    name: Mapped[str] = mapped_column(String(80), unique=True, index=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class Permission(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "permissions"

    code: Mapped[str] = mapped_column(String(120), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    resource_type: Mapped[str] = mapped_column(String(64), default="platform", nullable=False)


class UserRole(Base):
    __tablename__ = "user_roles"
    __table_args__ = (UniqueConstraint("user_id", "role_id", name="uq_user_role"),)

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role_id: Mapped[str] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )


class RolePermission(Base):
    __tablename__ = "role_permissions"
    __table_args__ = (UniqueConstraint("role_id", "permission_id", name="uq_role_permission"),)

    role_id: Mapped[str] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )
    permission_id: Mapped[str] = mapped_column(
        ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True
    )


class ResourcePermission(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "resource_permissions"
    __table_args__ = (
        UniqueConstraint(
            "resource_type",
            "resource_id",
            "subject_type",
            "subject_id",
            name="uq_resource_permission",
        ),
    )

    resource_type: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    resource_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    subject_type: Mapped[str] = mapped_column(String(16), nullable=False)  # user | role
    subject_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    actions: Mapped[list[str]] = mapped_column(default=list, nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)


class AccessPolicy(UUIDMixin, TimestampMixin, Base):
    """Attribute-based policy evaluated before owner/grant checks.

    Policies are deny-by-default only when they explicitly match; the existing
    owner/resource-grant rules remain as the RBAC fallback.
    """

    __tablename__ = "access_policies"

    name: Mapped[str] = mapped_column(String(120), unique=True, index=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    effect: Mapped[str] = mapped_column(String(8), default="allow", nullable=False)
    resource_type: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    action: Mapped[str] = mapped_column(String(32), default="*", nullable=False)
    subject_roles: Mapped[list[str]] = mapped_column(default=list, nullable=False)
    subject_departments: Mapped[list[str]] = mapped_column(default=list, nullable=False)
    tenant_ids: Mapped[list[str]] = mapped_column(default=list, nullable=False)
    resource_ids: Mapped[list[str]] = mapped_column(default=list, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
