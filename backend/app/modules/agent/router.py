from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import require_permission
from app.core.contracts import UserContext
from app.infrastructure.database.session import get_db
from app.modules.agent.schemas import (
    AgentCreate,
    AgentOut,
    AgentUpdate,
    AgentValidationResult,
    AgentVersionOut,
)
from app.modules.agent.service import AgentService

router = APIRouter(tags=["agents"])


@router.get("/agents", response_model=list[AgentOut])
async def list_agents(
    user: UserContext = Depends(require_permission("agents:read")),
    db: AsyncSession = Depends(get_db),
):
    return await AgentService(db).list_agents(user)


@router.post("/agents", response_model=AgentOut, status_code=201)
async def create_agent(
    payload: AgentCreate,
    user: UserContext = Depends(require_permission("agents:write")),
    db: AsyncSession = Depends(get_db),
):
    return await AgentService(db).create_agent(payload, user)


@router.get("/agents/{agent_id}", response_model=AgentOut)
async def get_agent(
    agent_id: str,
    user: UserContext = Depends(require_permission("agents:read")),
    db: AsyncSession = Depends(get_db),
):
    return await AgentService(db).get_agent(agent_id, user)


@router.put("/agents/{agent_id}", response_model=AgentOut)
async def update_agent(
    agent_id: str,
    payload: AgentUpdate,
    user: UserContext = Depends(require_permission("agents:write")),
    db: AsyncSession = Depends(get_db),
):
    return await AgentService(db).update_agent(agent_id, payload, user)


@router.delete("/agents/{agent_id}", status_code=204)
async def delete_agent(
    agent_id: str,
    user: UserContext = Depends(require_permission("agents:write")),
    db: AsyncSession = Depends(get_db),
):
    await AgentService(db).delete_agent(agent_id, user)


@router.post("/agents/{agent_id}/validate", response_model=AgentValidationResult)
async def validate_agent(
    agent_id: str,
    user: UserContext = Depends(require_permission("agents:read")),
    db: AsyncSession = Depends(get_db),
):
    return await AgentService(db).validate(agent_id, user)


@router.post("/agents/{agent_id}/publish", response_model=AgentOut)
async def publish_agent(
    agent_id: str,
    user: UserContext = Depends(require_permission("agents:publish")),
    db: AsyncSession = Depends(get_db),
):
    return await AgentService(db).publish(agent_id, user)


@router.post("/agents/{agent_id}/disable", response_model=AgentOut)
async def disable_agent(
    agent_id: str,
    user: UserContext = Depends(require_permission("agents:publish")),
    db: AsyncSession = Depends(get_db),
):
    return await AgentService(db).disable(agent_id, user)


@router.get("/agents/{agent_id}/versions", response_model=list[AgentVersionOut])
async def list_agent_versions(
    agent_id: str,
    user: UserContext = Depends(require_permission("agents:read")),
    db: AsyncSession = Depends(get_db),
):
    return await AgentService(db).list_versions(agent_id, user)
