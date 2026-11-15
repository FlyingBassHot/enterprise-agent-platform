from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.audit_middleware import AuditMiddleware
from app.core.config import get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import RequestContextMiddleware, configure_logging, get_logger
from app.core.rate_limit import RateLimitMiddleware
from app.domain_plugins.audit import register as register_audit_plugin
from app.infrastructure.database.session import check_database, get_session_factory, init_db
from app.modules.execution.service import ExecutionService
from app.modules.permission.service import PermissionService
from app.modules.runtime.checkpoint import close_checkpointer
from app.modules.tool.service import ToolService

configure_logging()
logger = get_logger("app.main")
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not settings.worker_inline:
        raise RuntimeError(
            "APP_WORKER_INLINE=false requires an external worker; none is bundled in this build"
        )
    await init_db()
    async with get_session_factory()() as db:
        service = PermissionService(db)
        await service.ensure_seed()
        register_audit_plugin()
        await ToolService(db).sync_native()
        if settings.bootstrap_admin_username and settings.bootstrap_admin_password:
            if await service.count_users() == 0:
                await service.bootstrap_admin(
                    settings.bootstrap_admin_username,
                    settings.bootstrap_admin_password,
                    display_name="Administrator",
                )
                logger.info("bootstrap admin created")
        recovered = await ExecutionService(db).recover_stale_executions()
        if recovered:
            logger.info("recovered stale executions", extra={"event": "recovery"})
    logger.info("application started", extra={"event": "startup"})
    yield
    await close_checkpointer()
    logger.info("application stopped", extra={"event": "shutdown"})


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.name,
        version="0.1.0",
        description="Enterprise Agent Platform API",
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(AuditMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    register_exception_handlers(app)
    app.include_router(api_router, prefix=settings.api_prefix)

    @app.get("/health", tags=["system"])
    async def health() -> dict[str, object]:
        return {
            "status": "ok" if await check_database() else "degraded",
            "database": "ok" if await check_database() else "error",
            "environment": settings.env,
            "version": "0.1.0",
        }

    return app


app = create_app()
