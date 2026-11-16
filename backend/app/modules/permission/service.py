from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.contracts import UserContext
from app.core.exceptions import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from app.core.security import create_access_token, hash_password, verify_password
from app.modules.permission.models import AccessPolicy, Permission, Role, User
from app.modules.permission.repository import PermissionRepository
from app.modules.permission.schemas import (
    AccessPolicyCreate,
    AccessPolicyOut,
    AccessPolicyUpdate,
    PermissionOut,
    RoleCreate,
    RoleOut,
    RoleUpdate,
    UserCreate,
    UserOut,
    UserUpdate,
)

SYSTEM_PERMISSIONS: list[tuple[str, str, str]] = [
    ("agents:read", "查看 Agent", "agent"),
    ("agents:write", "编辑 Agent", "agent"),
    ("agents:publish", "发布 Agent", "agent"),
    ("prompts:read", "查看 Prompt", "prompt"),
    ("prompts:write", "编辑 Prompt", "prompt"),
    ("models:read", "查看模型配置", "model"),
    ("models:write", "编辑模型配置", "model"),
    ("tools:read", "查看工具", "tool"),
    ("tools:write", "编辑工具", "tool"),
    ("tools:execute", "执行工具", "tool"),
    ("tools:admin", "工具安全策略管理", "tool"),
    ("executions:read", "查看执行记录", "execution"),
    ("executions:write", "创建/取消执行", "execution"),
    ("approvals:read", "查看审批", "approval"),
    ("approvals:write", "处理审批", "approval"),
    ("approvals:read_all", "查看所有审批", "approval"),
    ("approvals:override", "审批例外/自批", "approval"),
    ("users:manage", "用户管理", "user"),
    ("roles:manage", "角色管理", "role"),
    ("dashboard:read", "查看仪表盘", "platform"),
    ("audit:read", "访问审计演示", "audit"),
    ("knowledge:read", "查看知识库", "knowledge"),
    ("knowledge:write", "管理知识库与文档", "knowledge"),
    ("skills:read", "查看 Skill", "skill"),
    ("skills:write", "同步与启停 Skill", "skill"),
    ("workflows:read", "查看工作流", "workflow"),
    ("workflows:write", "编辑工作流", "workflow"),
    ("workflows:publish", "发布工作流", "workflow"),
    ("audit:logs:read", "查看审计日志", "audit"),
]

_BOOTSTRAP_LOCKS: dict[int, asyncio.Lock] = {}


def _bootstrap_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    return _BOOTSTRAP_LOCKS.setdefault(id(loop), asyncio.Lock())

ROLE_PERMISSIONS: dict[str, set[str] | None] = {
    "admin": None,  # all
    "developer": {
        "agents:read",
        "agents:write",
        "agents:publish",
        "prompts:read",
        "prompts:write",
        "models:read",
        "models:write",
        "tools:read",
        "tools:write",
        "tools:execute",
        "executions:read",
        "executions:write",
        "approvals:read",
        "approvals:write",
        "dashboard:read",
        "audit:read",
        "knowledge:read",
        "knowledge:write",
        "skills:read",
        "workflows:read",
        "workflows:write",
        "workflows:publish",
    },
    "viewer": {
        "agents:read",
        "prompts:read",
        "models:read",
        "tools:read",
        "executions:read",
        "approvals:read",
        "dashboard:read",
        "audit:read",
        "knowledge:read",
        "skills:read",
        "workflows:read",
    },
    "operator": {
        "agents:read",
        "executions:read",
        "executions:write",
        "tools:read",
        "tools:execute",
        "approvals:read",
        "dashboard:read",
        "audit:read",
        "knowledge:read",
        "skills:read",
        "workflows:read",
    },
    "approver": {
        "agents:read",
        "executions:read",
        "approvals:read",
        "approvals:write",
        "approvals:read_all",
        "dashboard:read",
        "audit:read",
    },
}


class PermissionService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = PermissionRepository(db)

    async def ensure_seed(self) -> None:
        await self._upsert_permissions()
        existing_roles = {role.name: role for role in await self.repo.list_roles()}
        all_permissions = {p.code: p for p in await self.repo.list_permissions()}
        for role_name, permission_codes in ROLE_PERMISSIONS.items():
            role = existing_roles.get(role_name)
            if role is None:
                role = Role(
                    name=role_name,
                    description={
                        "admin": "平台管理员",
                        "developer": "Agent 开发者",
                        "viewer": "只读用户",
                        "operator": "业务操作员（运行 Agent）",
                        "approver": "审批人（可处理他人审批）",
                    }[role_name],
                    is_system=True,
                )
                self.db.add(role)
                await self.db.flush()
            codes = set(all_permissions) if permission_codes is None else permission_codes
            if role_name == "admin" or role_name not in existing_roles:
                await self.repo.set_role_permissions(
                    role.id, [all_permissions[code].id for code in codes if code in all_permissions]
                )
        await self.db.commit()

    async def _upsert_permissions(self) -> None:
        existing = {p.code for p in await self.repo.list_permissions()}
        for code, name, resource_type in SYSTEM_PERMISSIONS:
            if code not in existing:
                self.db.add(
                    Permission(
                        code=code, name=name, resource_type=resource_type, description=name
                    )
                )
        await self.db.flush()

    async def count_users(self) -> int:
        return await self.repo.count_users()

    async def bootstrap_admin(
        self, username: str, password: str, display_name: str = "", email: str | None = None
    ) -> UserOut:
        async with _bootstrap_lock():
            await self.db.rollback()
            if await self.repo.count_users() > 0:
                raise ConflictError("Platform already initialized")
            await self.ensure_seed()
            admin_role = await self.repo.get_role_by_name("admin")
            if admin_role is None:
                raise ConflictError("Admin role is not initialized")
            user = User(
                username=username,
                password_hash=hash_password(password),
                display_name=display_name or username,
                email=email,
                is_superuser=True,
            )
            self.db.add(user)
            await self.db.flush()
            await self.repo.set_user_roles(user.id, [admin_role.id])
            await self.db.commit()
            return await self._user_out(user)

    async def authenticate(self, username: str, password: str) -> User:
        user = await self.repo.get_user_by_username(username)
        if user is None or not verify_password(password, user.password_hash):
            raise AuthenticationError("Invalid username or password")
        if not user.is_active:
            raise AuthenticationError("User is disabled")
        user.last_login_at = datetime.now(UTC)
        await self.db.commit()
        return user

    async def issue_token(self, user: User) -> tuple[str, int]:
        from app.core.config import get_settings

        settings = get_settings()
        token = create_access_token(user.id, {"username": user.username})
        return token, settings.access_token_ttl_minutes * 60

    async def load_context(self, user_id: str) -> UserContext:
        user = await self.repo.get_user(user_id)
        if user is None or not user.is_active:
            raise AuthenticationError("User not found or disabled")
        role_names = await self.repo.user_role_names(user.id)
        permissions = await self.repo.user_permission_codes(user.id)
        return UserContext(
            user_id=user.id,
            username=user.username,
            display_name=user.display_name,
            is_superuser=user.is_superuser,
            roles=role_names,
            permissions=permissions,
            department_id=user.department_id,
            tenant_id=user.tenant_id,
        )

    async def user_out(self, user: User) -> UserOut:
        return await self._user_out(user)

    async def _user_out(self, user: User) -> UserOut:
        role_names = await self.repo.user_role_names(user.id)
        return UserOut(
            id=user.id,
            username=user.username,
            email=user.email,
            display_name=user.display_name,
            is_active=user.is_active,
            is_superuser=user.is_superuser,
            department_id=user.department_id,
            roles=role_names,
            permissions=sorted(await self.repo.user_permission_codes(user.id)),
            created_at=user.created_at,
        )

    async def list_users(self) -> list[UserOut]:
        return [await self._user_out(user) for user in await self.repo.list_users()]

    async def create_user(
        self, payload: UserCreate, actor: UserContext | None = None
    ) -> UserOut:
        if payload.is_superuser and (actor is None or not actor.is_superuser):
            raise PermissionDeniedError("Only a superuser can create a superuser")
        if await self.repo.get_user_by_username(payload.username):
            raise ConflictError("Username already exists")
        role_ids = await self._resolve_role_ids(payload.role_names)
        user = User(
            username=payload.username,
            password_hash=hash_password(payload.password),
            display_name=payload.display_name or payload.username,
            email=payload.email,
            department_id=payload.department_id,
            is_superuser=payload.is_superuser,
        )
        self.db.add(user)
        await self.db.flush()
        await self.repo.set_user_roles(user.id, role_ids)
        await self.db.commit()
        return await self._user_out(user)

    async def update_user(
        self, user_id: str, payload: UserUpdate, actor: UserContext | None = None
    ) -> UserOut:
        user = await self.repo.get_user(user_id)
        if user is None:
            raise NotFoundError("User not found")
        if user.is_superuser and (actor is None or not actor.is_superuser):
            raise PermissionDeniedError("Only a superuser can modify a superuser")
        if user_id == (actor.user_id if actor else None) and payload.is_active is False:
            raise PermissionDeniedError("You cannot deactivate your own account")
        if payload.display_name is not None:
            user.display_name = payload.display_name
        if payload.email is not None:
            user.email = payload.email
        if payload.is_active is not None:
            user.is_active = payload.is_active
        if payload.department_id is not None:
            user.department_id = payload.department_id
        if payload.password:
            user.password_hash = hash_password(payload.password)
        if payload.role_names is not None:
            role_ids = await self._resolve_role_ids(payload.role_names)
            await self.repo.set_user_roles(user.id, role_ids)
        await self.db.commit()
        return await self._user_out(user)

    async def _resolve_role_ids(self, role_names: Iterable[str]) -> list[str]:
        role_ids: list[str] = []
        for name in role_names:
            role = await self.repo.get_role_by_name(name)
            if role is None:
                raise ValidationError(f"Unknown role: {name}")
            role_ids.append(role.id)
        return role_ids

    async def list_roles(self) -> list[RoleOut]:
        result: list[RoleOut] = []
        for role in await self.repo.list_roles():
            result.append(
                RoleOut(
                    id=role.id,
                    name=role.name,
                    description=role.description,
                    is_system=role.is_system,
                    permissions=await self.repo.role_permission_codes(role.id),
                    created_at=role.created_at,
                )
            )
        return result

    async def create_role(self, payload: RoleCreate) -> RoleOut:
        if await self.repo.get_role_by_name(payload.name):
            raise ConflictError("Role name already exists")
        permissions = await self.repo.permissions_by_codes(payload.permission_codes)
        if len(permissions) != len(set(payload.permission_codes)):
            raise ValidationError("Unknown permission code in role definition")
        role = Role(name=payload.name, description=payload.description)
        self.db.add(role)
        await self.db.flush()
        await self.repo.set_role_permissions(role.id, [p.id for p in permissions])
        await self.db.commit()
        return RoleOut(
            id=role.id,
            name=role.name,
            description=role.description,
            is_system=role.is_system,
            permissions=sorted(payload.permission_codes),
            created_at=role.created_at,
        )

    async def update_role(self, role_id: str, payload: RoleUpdate) -> RoleOut:
        role = await self.repo.get_role(role_id)
        if role is None:
            raise NotFoundError("Role not found")
        if payload.description is not None:
            role.description = payload.description
        if payload.permission_codes is not None:
            if role.name == "admin":
                raise ValidationError("Admin role permissions cannot be reduced")
            permissions = await self.repo.permissions_by_codes(payload.permission_codes)
            if len(permissions) != len(set(payload.permission_codes)):
                raise ValidationError("Unknown permission code in role definition")
            await self.repo.set_role_permissions(role.id, [p.id for p in permissions])
        await self.db.commit()
        return RoleOut(
            id=role.id,
            name=role.name,
            description=role.description,
            is_system=role.is_system,
            permissions=await self.repo.role_permission_codes(role.id),
            created_at=role.created_at,
        )

    async def list_permissions(self) -> list[PermissionOut]:
        return [
            PermissionOut.model_validate(permission)
            for permission in await self.repo.list_permissions()
        ]

    async def require_permission(self, user: UserContext, code: str) -> None:
        if not user.has(code):
            raise PermissionDeniedError(f"Missing permission: {code}")

    async def policy_effect(
        self,
        user: UserContext,
        *,
        resource_type: str,
        resource_id: str,
        action: str,
    ) -> bool | None:
        """Return an explicit ABAC decision, or None when no policy matches."""

        matches: list[AccessPolicy] = []
        for policy in await self.repo.list_policies(enabled_only=True):
            if policy.resource_type not in {resource_type, "*"}:
                continue
            if policy.action not in {action, "*"}:
                continue
            if policy.subject_roles and not set(policy.subject_roles).intersection(user.roles):
                continue
            if policy.subject_departments and user.department_id not in policy.subject_departments:
                continue
            if policy.tenant_ids and user.tenant_id not in policy.tenant_ids:
                continue
            if policy.resource_ids and resource_id not in policy.resource_ids:
                continue
            matches.append(policy)
        if not matches:
            return None
        matches.sort(key=lambda item: (-item.priority, 0 if item.effect == "deny" else 1))
        return matches[0].effect == "allow"

    async def can_access_resource(
        self,
        user: UserContext,
        *,
        resource_type: str,
        resource_id: str,
        action: str = "read",
        owner_id: str | None = None,
    ) -> bool:
        if user.is_superuser:
            return True
        effect = await self.policy_effect(
            user,
            resource_type=resource_type,
            resource_id=resource_id,
            action=action,
        )
        if effect is not None:
            return effect
        if user.has(f"{resource_type}:manage"):
            return True
        if owner_id and owner_id == user.user_id:
            return True
        grants = await self.repo.resource_permissions_for(resource_type, resource_id)
        role_names = set(user.roles)
        for grant in grants:
            if grant.subject_type == "user":
                if grant.subject_id != user.user_id:
                    continue
            elif grant.subject_type == "role":
                if grant.subject_id not in role_names:
                    continue
            else:
                continue
            if "*" in grant.actions or action in grant.actions:
                return True
        return False

    async def decide_resource_access(
        self,
        user: UserContext,
        *,
        resource_type: str,
        resource_id: str,
        action: str = "read",
        owner_id: str | None = None,
    ):
        """Structured verdict; the raising helper below delegates to this."""

        from app.core.policy import allow, deny

        allowed = await self.can_access_resource(
            user,
            resource_type=resource_type,
            resource_id=resource_id,
            action=action,
            owner_id=owner_id,
        )
        if allowed:
            return allow(f"{action} {resource_type} {resource_id}")
        return deny(
            "permission_denied",
            f"No {action} access to {resource_type} {resource_id}",
        )

    async def require_resource_access(
        self,
        user: UserContext,
        *,
        resource_type: str,
        resource_id: str,
        action: str = "read",
        owner_id: str | None = None,
    ) -> None:
        decision = await self.decide_resource_access(
            user,
            resource_type=resource_type,
            resource_id=resource_id,
            action=action,
            owner_id=owner_id,
        )
        if not decision.allowed:
            raise PermissionDeniedError(decision.reason)

    async def list_policies(self, actor: UserContext) -> list[AccessPolicyOut]:
        await self.require_permission(actor, "roles:manage")
        return [
            AccessPolicyOut.model_validate(policy)
            for policy in await self.repo.list_policies()
        ]

    async def create_policy(
        self, actor: UserContext, payload: AccessPolicyCreate
    ) -> AccessPolicyOut:
        await self.require_permission(actor, "roles:manage")
        if await self.repo.get_policy_by_name(payload.name):
            raise ConflictError("Access policy name already exists")
        policy = AccessPolicy(**payload.model_dump())
        self.db.add(policy)
        await self.db.commit()
        await self.db.refresh(policy)
        return AccessPolicyOut.model_validate(policy)

    async def update_policy(
        self, actor: UserContext, policy_id: str, payload: AccessPolicyUpdate
    ) -> AccessPolicyOut:
        await self.require_permission(actor, "roles:manage")
        policy = await self.repo.get_policy(policy_id)
        if policy is None:
            raise NotFoundError("Access policy not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            if value is not None:
                setattr(policy, key, value)
        await self.db.commit()
        return AccessPolicyOut.model_validate(policy)

    async def delete_policy(self, actor: UserContext, policy_id: str) -> None:
        await self.require_permission(actor, "roles:manage")
        policy = await self.repo.get_policy(policy_id)
        if policy is None:
            raise NotFoundError("Access policy not found")
        await self.db.delete(policy)
        await self.db.commit()

    async def grant(self, payload, created_by: str | None = None):  # noqa: ANN001
        grant = await self.repo.grant_resource_permission(
            resource_type=payload.resource_type,
            resource_id=payload.resource_id,
            subject_type=payload.subject_type,
            subject_id=payload.subject_id,
            actions=payload.actions,
            created_by=created_by,
        )
        await self.db.commit()
        return grant
