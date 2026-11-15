from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.permission.models import (
    Permission,
    ResourcePermission,
    Role,
    RolePermission,
    User,
    UserRole,
)


class PermissionRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_user_by_username(self, username: str) -> User | None:
        return await self.db.scalar(select(User).where(User.username == username))

    async def get_user(self, user_id: str) -> User | None:
        return await self.db.get(User, user_id)

    async def list_users(self) -> list[User]:
        return list((await self.db.scalars(select(User).order_by(User.created_at))).all())

    async def user_role_names(self, user_id: str) -> list[str]:
        rows = await self.db.execute(
            select(Role.name).join(UserRole, UserRole.role_id == Role.id).where(
                UserRole.user_id == user_id
            )
        )
        return [row[0] for row in rows.all()]

    async def user_roles(self, user_id: str) -> list[Role]:
        rows = await self.db.execute(
            select(Role).join(UserRole, UserRole.role_id == Role.id).where(
                UserRole.user_id == user_id
            )
        )
        return list(rows.scalars().all())

    async def user_permission_codes(self, user_id: str) -> set[str]:
        rows = await self.db.execute(
            select(Permission.code)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .join(UserRole, UserRole.role_id == RolePermission.role_id)
            .where(UserRole.user_id == user_id)
        )
        return {row[0] for row in rows.all()}

    async def list_roles(self) -> list[Role]:
        return list((await self.db.scalars(select(Role).order_by(Role.name))).all())

    async def get_role(self, role_id: str) -> Role | None:
        return await self.db.get(Role, role_id)

    async def get_role_by_name(self, name: str) -> Role | None:
        return await self.db.scalar(select(Role).where(Role.name == name))

    async def role_permission_codes(self, role_id: str) -> list[str]:
        rows = await self.db.execute(
            select(Permission.code)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .where(RolePermission.role_id == role_id)
        )
        return [row[0] for row in rows.all()]

    async def list_permissions(self) -> list[Permission]:
        return list((await self.db.scalars(select(Permission).order_by(Permission.code))).all())

    async def permissions_by_codes(self, codes: list[str]) -> list[Permission]:
        if not codes:
            return []
        return list(
            (await self.db.scalars(select(Permission).where(Permission.code.in_(codes)))).all()
        )

    async def set_user_roles(self, user_id: str, role_ids: list[str]) -> None:
        await self.db.execute(delete(UserRole).where(UserRole.user_id == user_id))
        for role_id in role_ids:
            self.db.add(UserRole(user_id=user_id, role_id=role_id))

    async def set_role_permissions(self, role_id: str, permission_ids: list[str]) -> None:
        await self.db.execute(delete(RolePermission).where(RolePermission.role_id == role_id))
        for permission_id in permission_ids:
            self.db.add(RolePermission(role_id=role_id, permission_id=permission_id))

    async def resource_permissions_for(
        self, resource_type: str, resource_id: str
    ) -> list[ResourcePermission]:
        return list(
            (
                await self.db.scalars(
                    select(ResourcePermission).where(
                        ResourcePermission.resource_type == resource_type,
                        ResourcePermission.resource_id == resource_id,
                    )
                )
            ).all()
        )

    async def grant_resource_permission(
        self, *, resource_type: str, resource_id: str, subject_type: str, subject_id: str,
        actions: list[str], created_by: str | None,
    ) -> ResourcePermission:
        existing = await self.db.scalar(
            select(ResourcePermission).where(
                ResourcePermission.resource_type == resource_type,
                ResourcePermission.resource_id == resource_id,
                ResourcePermission.subject_type == subject_type,
                ResourcePermission.subject_id == subject_id,
            )
        )
        if existing:
            existing.actions = actions
            return existing
        grant = ResourcePermission(
            resource_type=resource_type,
            resource_id=resource_id,
            subject_type=subject_type,
            subject_id=subject_id,
            actions=actions,
            created_by=created_by,
        )
        self.db.add(grant)
        return grant

    async def count_users(self) -> int:
        from sqlalchemy import func

        return int(await self.db.scalar(select(func.count()).select_from(User)) or 0)
