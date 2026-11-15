from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.model.models import ModelConfig, ModelProvider


class ModelRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_providers(self) -> list[ModelProvider]:
        return list(
            (await self.db.scalars(select(ModelProvider).order_by(ModelProvider.name))).all()
        )

    async def get_provider(self, provider_id: str) -> ModelProvider | None:
        return await self.db.get(ModelProvider, provider_id)

    async def get_provider_by_name(self, name: str) -> ModelProvider | None:
        return await self.db.scalar(select(ModelProvider).where(ModelProvider.name == name))

    async def list_configs(self) -> list[ModelConfig]:
        return list((await self.db.scalars(select(ModelConfig).order_by(ModelConfig.name))).all())

    async def get_config(self, config_id: str) -> ModelConfig | None:
        return await self.db.get(ModelConfig, config_id)

    async def get_config_by_name(self, name: str) -> ModelConfig | None:
        return await self.db.scalar(select(ModelConfig).where(ModelConfig.name == name))

    async def get_default_config(self) -> ModelConfig | None:
        return await self.db.scalar(
            select(ModelConfig).where(
                ModelConfig.is_default.is_(True), ModelConfig.enabled.is_(True)
            )
        )

    async def get_first_enabled(self) -> ModelConfig | None:
        return await self.db.scalar(
            select(ModelConfig).where(ModelConfig.enabled.is_(True)).order_by(ModelConfig.created_at)
        )

    async def clear_defaults(self, except_id: str | None = None) -> None:
        for config in await self.list_configs():
            if config.is_default and config.id != except_id:
                config.is_default = False
