from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import require_permission
from app.core.contracts import UserContext
from app.infrastructure.database.session import get_db
from app.modules.agent.service import AgentService
from app.modules.execution.insights import execution_stats
from app.modules.tool.service import ToolService

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


class RecentExecution(BaseModel):
    id: str
    agent_id: str
    agent_name: str | None = None
    user_id: str
    status: str
    input_text: str
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None
    tokens_total: int = 0


class DashboardStats(BaseModel):
    agents_total: int
    agents_published: int
    tools_total: int
    tools_enabled: int
    executions_total: int
    executions_completed: int
    executions_failed: int
    executions_running: int
    success_rate: float
    tokens_total: int
    avg_duration_ms: float | None = None
    status_breakdown: dict[str, int] = Field(default_factory=dict)
    recent_executions: list[RecentExecution] = Field(default_factory=list)


@router.get("/stats", response_model=DashboardStats)
async def dashboard_stats(
    actor: UserContext = Depends(
        require_permission("dashboard:read", "agents:read", "tools:read", "executions:read")
    ),
    db: AsyncSession = Depends(get_db),
):
    agents = await AgentService(db).list_agents(actor)
    tools = await ToolService(db).list_tools(actor)
    stats = await execution_stats(db, actor)
    names = {agent.id: agent.name for agent in agents}
    recent = [
        RecentExecution(**{**item, "agent_name": names.get(item["agent_id"])})
        for item in stats.pop("recent_executions")
    ]
    stats.pop("terminal_statuses", None)
    return DashboardStats(
        agents_total=len(agents),
        agents_published=sum(1 for agent in agents if agent.status == "published"),
        tools_total=len(tools),
        tools_enabled=sum(1 for tool in tools if tool.enabled),
        **stats,
        recent_executions=recent,
    )
