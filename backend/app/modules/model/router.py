from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import require_permission, require_superuser
from app.core.contracts import UserContext
from app.infrastructure.database.session import get_db
from app.modules.model.schemas import (
    ModelConfigCreate,
    ModelConfigOut,
    ModelConfigUpdate,
    ModelHealthOut,
    ProviderCreate,
    ProviderOut,
    ProviderUpdate,
)
from app.modules.model.service import ModelService

router = APIRouter(tags=["models"])


@router.get("/model-providers", response_model=list[ProviderOut])
async def list_providers(
    _: UserContext = Depends(require_permission("models:read")),
    db: AsyncSession = Depends(get_db),
):
    return await ModelService(db).list_providers()


@router.post("/model-providers", response_model=ProviderOut, status_code=201)
async def create_provider(
    payload: ProviderCreate,
    _: UserContext = Depends(require_superuser),
    db: AsyncSession = Depends(get_db),
):
    return await ModelService(db).create_provider(payload)


@router.patch("/model-providers/{provider_id}", response_model=ProviderOut)
async def update_provider(
    provider_id: str,
    payload: ProviderUpdate,
    _: UserContext = Depends(require_superuser),
    db: AsyncSession = Depends(get_db),
):
    return await ModelService(db).update_provider(provider_id, payload)


@router.delete("/model-providers/{provider_id}", status_code=204)
async def delete_provider(
    provider_id: str,
    _: UserContext = Depends(require_superuser),
    db: AsyncSession = Depends(get_db),
):
    await ModelService(db).delete_provider(provider_id)


@router.get("/model-configs", response_model=list[ModelConfigOut])
async def list_configs(
    _: UserContext = Depends(require_permission("models:read")),
    db: AsyncSession = Depends(get_db),
):
    return await ModelService(db).list_configs()


@router.post("/model-configs", response_model=ModelConfigOut, status_code=201)
async def create_config(
    payload: ModelConfigCreate,
    _: UserContext = Depends(require_permission("models:write")),
    db: AsyncSession = Depends(get_db),
):
    return await ModelService(db).create_config(payload)


@router.get("/model-configs/{config_id}", response_model=ModelConfigOut)
async def get_config(
    config_id: str,
    _: UserContext = Depends(require_permission("models:read")),
    db: AsyncSession = Depends(get_db),
):
    return await ModelService(db).get_config(config_id)


@router.patch("/model-configs/{config_id}", response_model=ModelConfigOut)
async def update_config(
    config_id: str,
    payload: ModelConfigUpdate,
    _: UserContext = Depends(require_permission("models:write")),
    db: AsyncSession = Depends(get_db),
):
    return await ModelService(db).update_config(config_id, payload)


@router.delete("/model-configs/{config_id}", status_code=204)
async def delete_config(
    config_id: str,
    _: UserContext = Depends(require_permission("models:write")),
    db: AsyncSession = Depends(get_db),
):
    await ModelService(db).delete_config(config_id)


@router.post("/model-configs/{config_id}/health", response_model=ModelHealthOut)
async def health_check(
    config_id: str,
    _: UserContext = Depends(require_permission("models:read")),
    db: AsyncSession = Depends(get_db),
):
    return await ModelService(db).health(config_id)
