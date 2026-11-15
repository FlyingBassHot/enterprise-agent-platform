from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.dashboard import router as dashboard_router
from app.api.v1.workflow_execute import router as workflow_execute_router
from app.domain_plugins.audit.router import router as audit_router
from app.modules.agent.router import router as agent_router
from app.modules.audit_log.router import router as audit_log_router
from app.modules.execution.router import router as execution_router
from app.modules.knowledge.router import router as knowledge_router
from app.modules.model.router import router as model_router
from app.modules.permission.router import auth_router, users_router
from app.modules.prompt.router import router as prompt_router
from app.modules.skill.router import router as skill_router
from app.modules.tool.router import mcp_router
from app.modules.tool.router import router as tool_router
from app.modules.workflow.router import router as workflow_router

api_router = APIRouter()
api_router.include_router(auth_router)
api_router.include_router(users_router)
api_router.include_router(model_router)
api_router.include_router(agent_router)
api_router.include_router(prompt_router)
api_router.include_router(tool_router)
api_router.include_router(mcp_router)
api_router.include_router(execution_router)
api_router.include_router(knowledge_router)
api_router.include_router(skill_router)
api_router.include_router(audit_router)
api_router.include_router(dashboard_router)
api_router.include_router(audit_log_router)
api_router.include_router(workflow_router)
api_router.include_router(workflow_execute_router)

__all__ = ["api_router"]
