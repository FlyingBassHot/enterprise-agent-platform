from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import require_permission
from app.core.contracts import UserContext
from app.infrastructure.database.session import get_db
from app.modules.prompt.schemas import (
    PromptCreate,
    PromptOut,
    PromptResolveRequest,
    PromptUpdate,
    PromptVersionCreate,
    PromptVersionOut,
    ResolvedPrompt,
)
from app.modules.prompt.service import PromptService

router = APIRouter(tags=["prompts"])


@router.get("/prompts", response_model=list[PromptOut])
async def list_prompts(
    _: UserContext = Depends(require_permission("prompts:read")),
    db: AsyncSession = Depends(get_db),
):
    return await PromptService(db).list_prompts()


@router.post("/prompts", response_model=PromptOut, status_code=201)
async def create_prompt(
    payload: PromptCreate,
    _: UserContext = Depends(require_permission("prompts:write")),
    db: AsyncSession = Depends(get_db),
):
    return await PromptService(db).create_prompt(payload)


@router.get("/prompts/{prompt_id}", response_model=PromptOut)
async def get_prompt(
    prompt_id: str,
    _: UserContext = Depends(require_permission("prompts:read")),
    db: AsyncSession = Depends(get_db),
):
    return await PromptService(db).get_prompt(prompt_id)


@router.put("/prompts/{prompt_id}", response_model=PromptOut)
async def update_prompt(
    prompt_id: str,
    payload: PromptUpdate,
    _: UserContext = Depends(require_permission("prompts:write")),
    db: AsyncSession = Depends(get_db),
):
    return await PromptService(db).update_prompt(prompt_id, payload)


@router.delete("/prompts/{prompt_id}", status_code=204)
async def delete_prompt(
    prompt_id: str,
    _: UserContext = Depends(require_permission("prompts:write")),
    db: AsyncSession = Depends(get_db),
):
    await PromptService(db).delete_prompt(prompt_id)


@router.post("/prompts/{prompt_id}/versions", response_model=PromptVersionOut, status_code=201)
async def create_version(
    prompt_id: str,
    payload: PromptVersionCreate,
    user: UserContext = Depends(require_permission("prompts:write")),
    db: AsyncSession = Depends(get_db),
):
    return await PromptService(db).create_version(prompt_id, payload, user.user_id)


@router.get("/prompts/{prompt_id}/versions", response_model=list[PromptVersionOut])
async def list_versions(
    prompt_id: str,
    _: UserContext = Depends(require_permission("prompts:read")),
    db: AsyncSession = Depends(get_db),
):
    return await PromptService(db).list_versions(prompt_id)


@router.post("/prompts/{prompt_id}/resolve", response_model=ResolvedPrompt)
async def resolve_prompt(
    prompt_id: str,
    payload: PromptResolveRequest,
    _: UserContext = Depends(require_permission("prompts:read")),
    db: AsyncSession = Depends(get_db),
):
    return await PromptService(db).resolve(prompt_id, payload.version_id, payload.variables)
