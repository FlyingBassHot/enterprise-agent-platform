from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import require_permission
from app.core.contracts import UserContext
from app.infrastructure.database.session import get_db
from app.modules.skill.schemas import SkillDetailOut, SkillOut, SkillVersionOut
from app.modules.skill.service import SkillService

router = APIRouter(prefix="/skills", tags=["skills"])


@router.get("", response_model=list[SkillOut])
async def list_skills(
    user: UserContext = Depends(require_permission("skills:read")),
    db: AsyncSession = Depends(get_db),
):
    return await SkillService(db).list_skills(user)


@router.post("/sync", response_model=list[SkillOut])
async def sync_skills(
    user: UserContext = Depends(require_permission("skills:write")),
    db: AsyncSession = Depends(get_db),
):
    return await SkillService(db).sync(user)


@router.get("/{skill_id}", response_model=SkillDetailOut)
async def get_skill(
    skill_id: str,
    _: UserContext = Depends(require_permission("skills:read")),
    db: AsyncSession = Depends(get_db),
):
    return await SkillService(db).get_skill(skill_id)


@router.post("/{skill_id}/enable", response_model=SkillOut)
async def enable_skill(
    skill_id: str,
    user: UserContext = Depends(require_permission("skills:write")),
    db: AsyncSession = Depends(get_db),
):
    return await SkillService(db).set_enabled(skill_id, True, user)


@router.post("/{skill_id}/disable", response_model=SkillOut)
async def disable_skill(
    skill_id: str,
    user: UserContext = Depends(require_permission("skills:write")),
    db: AsyncSession = Depends(get_db),
):
    return await SkillService(db).set_enabled(skill_id, False, user)


@router.get("/{skill_id}/versions", response_model=list[SkillVersionOut])
async def list_skill_versions(
    skill_id: str,
    _: UserContext = Depends(require_permission("skills:read")),
    db: AsyncSession = Depends(get_db),
):
    return await SkillService(db).list_versions(skill_id)
