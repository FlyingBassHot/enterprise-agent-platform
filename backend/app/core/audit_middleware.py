from __future__ import annotations

import time
import uuid
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.core.config import get_settings
from app.core.logging import get_logger, request_id_var
from app.core.security import decode_access_token
from app.infrastructure.database.session import get_session_factory

logger = get_logger(__name__)

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_AUTH_PATHS = {"/api/v1/auth/login", "/api/v1/auth/bootstrap"}


def _actor(request: Request) -> tuple[str | None, str]:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return None, ""
    try:
        payload = decode_access_token(header[7:].strip())
    except Exception:  # noqa: BLE001 - rejected tokens are audited as anonymous
        return None, ""
    return str(payload.get("sub") or "") or None, str(payload.get("username") or "")


def _resource(path: str) -> tuple[str, str | None]:
    parts = [part for part in path.split("/") if part]
    if len(parts) >= 3 and parts[0] == "api" and parts[1] == "v1":
        resource_type = parts[2]
        resource_id = next(
            (part for part in parts[3:] if len(part) >= 16 and "-" in part), None
        )
        return resource_type, resource_id
    return "", None


class AuditMiddleware(BaseHTTPMiddleware):
    """Append-only HTTP audit trail for mutating API calls.

    Login/bootstrap are recorded explicitly by the auth router so the actor is
    known; this middleware covers all other mutations including 403/422/500
    outcomes.
    """

    async def dispatch(self, request: Request, call_next: Any):
        settings = get_settings()
        path = request.url.path
        if (
            not settings.audit_log_enabled
            or request.method in _SAFE_METHODS
            or path in _AUTH_PATHS
        ):
            return await call_next(request)

        started = time.perf_counter()
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        actor_id, actor_username = _actor(request)
        resource_type, resource_id = _resource(path)
        status = response.status_code
        result = "success" if status < 400 else ("denied" if status in {401, 403} else "failure")
        try:
            from app.modules.audit_log.service import AuditService

            async with get_session_factory()() as db:
                await AuditService(db).record(
                    actor_id=actor_id,
                    actor_username=actor_username,
                    action=f"{request.method} {path}",
                    resource_type=resource_type,
                    resource_id=resource_id,
                    result=result,
                    status_code=status,
                    ip_address=request.client.host if request.client else None,
                    request_id=request_id_var.get() or str(uuid.uuid4()),
                    details={
                        "method": request.method,
                        "path": path,
                        "status_code": status,
                        "duration_ms": duration_ms,
                        "user_agent": (request.headers.get("user-agent") or "")[:300],
                    },
                )
        except Exception:  # noqa: BLE001 - auditing must never break the response
            logger.exception("failed to write audit log", extra={"event": "audit_failure"})
        return response


__all__ = ["AuditMiddleware"]
