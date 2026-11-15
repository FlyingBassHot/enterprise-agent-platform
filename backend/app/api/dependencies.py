from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.contracts import UserContext
from app.core.exceptions import AuthenticationError, PermissionDeniedError
from app.core.security import decode_access_token
from app.infrastructure.database.session import get_db
from app.modules.permission.service import PermissionService

_bearer = HTTPBearer(auto_error=False)


def get_permission_service(db: AsyncSession = Depends(get_db)) -> PermissionService:
    return PermissionService(db)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
) -> UserContext:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AuthenticationError("Missing bearer token")
    payload = decode_access_token(credentials.credentials)
    user_id = payload.get("sub")
    if not user_id:
        raise AuthenticationError("Invalid token subject")
    return await PermissionService(db).load_context(user_id)


def require_permission(*codes: str) -> Callable:
    async def _checker(user: UserContext = Depends(get_current_user)) -> UserContext:
        missing = [code for code in codes if not user.has(code)]
        if missing:
            raise PermissionDeniedError(f"Missing permission: {', '.join(missing)}")
        return user

    return _checker


async def require_superuser(user: UserContext = Depends(get_current_user)) -> UserContext:
    if not user.is_superuser:
        raise PermissionDeniedError("Administrator privileges are required")
    return user


def require_role(*roles: str) -> Callable:
    async def _checker(user: UserContext = Depends(get_current_user)) -> UserContext:
        if not user.is_superuser and not set(roles).intersection(user.roles):
            raise PermissionDeniedError("Insufficient role")
        return user

    return _checker
