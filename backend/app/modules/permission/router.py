from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_user, require_permission
from app.core.contracts import UserContext
from app.infrastructure.database.session import get_db
from app.modules.permission.schemas import (
    BootstrapRequest,
    LoginRequest,
    PermissionOut,
    ResourcePermissionGrant,
    RoleCreate,
    RoleOut,
    RoleUpdate,
    TokenResponse,
    UserCreate,
    UserOut,
    UserUpdate,
)
from app.modules.permission.service import PermissionService

auth_router = APIRouter(prefix="/auth", tags=["auth"])
users_router = APIRouter(tags=["identity"])


@auth_router.post("/bootstrap", response_model=TokenResponse)
async def bootstrap(payload: BootstrapRequest, db: AsyncSession = Depends(get_db)):
    import hmac

    from app.core.config import get_settings
    from app.core.exceptions import PermissionDeniedError

    settings = get_settings()
    if settings.bootstrap_token:
        supplied = payload.token or ""
        if not hmac.compare_digest(supplied, settings.bootstrap_token):
            raise PermissionDeniedError("Invalid bootstrap token")
    elif settings.env not in {"dev", "test"}:
        raise PermissionDeniedError(
            "Bootstrap requires APP_BOOTSTRAP_TOKEN outside dev/test"
        )
    service = PermissionService(db)
    user = await service.bootstrap_admin(
        payload.username, payload.password, payload.display_name, payload.email
    )
    token, expires_in = await service.issue_token(
        await service.repo.get_user(user.id)  # type: ignore[arg-type]
    )
    return TokenResponse(access_token=token, expires_in=expires_in, user=user)


@auth_router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)):
    service = PermissionService(db)
    user = await service.authenticate(payload.username, payload.password)
    token, expires_in = await service.issue_token(user)
    return TokenResponse(
        access_token=token, expires_in=expires_in, user=await service.user_out(user)
    )


@auth_router.get("/me", response_model=UserOut)
async def me(user: UserContext = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    service = PermissionService(db)
    row = await service.repo.get_user(user.user_id)
    if row is None:
        from app.core.exceptions import NotFoundError

        raise NotFoundError("User not found")
    return await service.user_out(row)


@users_router.get("/users", response_model=list[UserOut])
async def list_users(
    _: UserContext = Depends(require_permission("users:manage")),
    db: AsyncSession = Depends(get_db),
):
    return await PermissionService(db).list_users()


@users_router.post("/users", response_model=UserOut, status_code=201)
async def create_user(
    payload: UserCreate,
    _: UserContext = Depends(require_permission("users:manage")),
    db: AsyncSession = Depends(get_db),
):
    return await PermissionService(db).create_user(payload)


@users_router.patch("/users/{user_id}", response_model=UserOut)
async def update_user(
    user_id: str,
    payload: UserUpdate,
    _: UserContext = Depends(require_permission("users:manage")),
    db: AsyncSession = Depends(get_db),
):
    return await PermissionService(db).update_user(user_id, payload)


@users_router.get("/roles", response_model=list[RoleOut])
async def list_roles(
    _: UserContext = Depends(require_permission("roles:manage")),
    db: AsyncSession = Depends(get_db),
):
    return await PermissionService(db).list_roles()


@users_router.post("/roles", response_model=RoleOut, status_code=201)
async def create_role(
    payload: RoleCreate,
    _: UserContext = Depends(require_permission("roles:manage")),
    db: AsyncSession = Depends(get_db),
):
    return await PermissionService(db).create_role(payload)


@users_router.patch("/roles/{role_id}", response_model=RoleOut)
async def update_role(
    role_id: str,
    payload: RoleUpdate,
    _: UserContext = Depends(require_permission("roles:manage")),
    db: AsyncSession = Depends(get_db),
):
    return await PermissionService(db).update_role(role_id, payload)


@users_router.get("/permissions", response_model=list[PermissionOut])
async def list_permissions(
    _: UserContext = Depends(require_permission("roles:manage")),
    db: AsyncSession = Depends(get_db),
):
    return await PermissionService(db).list_permissions()


@users_router.post("/permissions/grant", status_code=201)
async def grant_permission(
    payload: ResourcePermissionGrant,
    user: UserContext = Depends(require_permission("roles:manage")),
    db: AsyncSession = Depends(get_db),
):
    grant = await PermissionService(db).grant(payload, user.user_id)
    return {
        "id": grant.id,
        "resource_type": grant.resource_type,
        "resource_id": grant.resource_id,
        "subject_type": grant.subject_type,
        "subject_id": grant.subject_id,
        "actions": grant.actions,
    }
