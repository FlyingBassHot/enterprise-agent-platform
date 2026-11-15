from __future__ import annotations

import time
from collections.abc import AsyncIterator, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.contracts import (
    ChatMessage,
    ChatResult,
    ModelConfigResolver,
    ModelGateway,
    ModelStreamEvent,
    ToolSpec,
)
from app.core.exceptions import ConflictError, ModelError, NotFoundError, ValidationError
from app.core.security import decrypt_secret, encrypt_secret
from app.infrastructure.model_gateway.base import BaseModelProvider
from app.infrastructure.model_gateway.mock import MockModelProvider
from app.infrastructure.model_gateway.openai_compatible import OpenAICompatibleProvider
from app.modules.model.models import ModelConfig, ModelProvider
from app.modules.model.repository import ModelRepository
from app.modules.model.schemas import (
    ModelCapabilities,
    ModelConfigCreate,
    ModelConfigOut,
    ModelConfigUpdate,
    ModelHealthOut,
    ModelParams,
    ProviderCreate,
    ProviderOut,
    ProviderUpdate,
)

PROVIDERS: dict[str, type[BaseModelProvider]] = {
    "mock": MockModelProvider,
    "openai_compatible": OpenAICompatibleProvider,
}


def _mask_extra(value):
    if isinstance(value, dict):
        return {
            key: "***"
            if any(part in key.lower() for part in ("token", "key", "secret", "password"))
            else _mask_extra(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_mask_extra(item) for item in value]
    return value


class ModelService(ModelGateway):
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = ModelRepository(db)

    # ------------------------------------------------------------------ resolve

    async def resolve(self, model_config_id: str | None) -> ModelConfigResolver:
        if model_config_id:
            config = await self.repo.get_config(model_config_id)
            if config is None:
                raise NotFoundError(f"Model config {model_config_id} not found")
            provider = await self.repo.get_provider(config.provider_id)
            if provider is None:
                raise NotFoundError("Model provider not found")
        else:
            config = await self.repo.get_default_config() or await self.repo.get_first_enabled()
            if config is None:
                return ModelConfigResolver(
                    provider_type="mock",
                    provider_name="builtin-mock",
                    model="mock-agent",
                    temperature=0.2,
                    max_tokens=2048,
                )
            provider = await self.repo.get_provider(config.provider_id)
            if provider is None:
                raise NotFoundError("Model provider not found")
        if not config.enabled:
            raise ModelError(f"Model config {config.name} is disabled")
        if not provider.enabled:
            raise ModelError(f"Model provider {provider.name} is disabled")
        params = config.params or {}
        extra = {**(provider.extra or {}), **(config.extra or {})}
        return ModelConfigResolver(
            config_id=config.id,
            provider_type=provider.provider_type,
            provider_name=provider.name,
            model=config.model_name,
            base_url=provider.base_url,
            temperature=float(params.get("temperature", 0.2)),
            max_tokens=int(params.get("max_tokens", 2048)),
            top_p=float(params.get("top_p", 1.0)),
            extra=extra,
        )

    async def _provider_instance(self, model_config_id: str | None) -> BaseModelProvider:
        resolved = await self.resolve(model_config_id)
        provider_cls = PROVIDERS.get(resolved.provider_type)
        if provider_cls is None:
            raise ModelError(f"Unsupported provider type: {resolved.provider_type}")
        api_key = ""
        if resolved.config_id:
            config = await self.repo.get_config(resolved.config_id)
            if config is not None:
                provider_row = await self.repo.get_provider(config.provider_id)
                if provider_row is not None:
                    api_key = decrypt_secret(provider_row.api_key_encrypted)
        return provider_cls(resolved, api_key)

    # ------------------------------------------------------------------- gateway

    async def chat(
        self,
        *,
        model_config_id: str | None,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec] = (),
        temperature: float | None = None,
        max_tokens: int | None = None,
        execution_id: str | None = None,
    ) -> ChatResult:
        provider = await self._provider_instance(model_config_id)
        return await provider.chat(
            messages, tools, temperature=temperature, max_tokens=max_tokens
        )

    async def stream_chat(
        self,
        *,
        model_config_id: str | None,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec] = (),
        temperature: float | None = None,
        max_tokens: int | None = None,
        execution_id: str | None = None,
    ) -> AsyncIterator[ModelStreamEvent]:
        provider = await self._provider_instance(model_config_id)
        async for event in provider.stream_chat(
            messages, tools, temperature=temperature, max_tokens=max_tokens
        ):
            yield event

    async def health(self, model_config_id: str) -> ModelHealthOut:
        started = time.perf_counter()
        try:
            provider = await self._provider_instance(model_config_id)
            ok, message = await provider.health()
        except (ModelError, NotFoundError) as exc:
            ok, message = False, str(exc)
        return ModelHealthOut(
            ok=ok,
            message=message,
            latency_ms=round((time.perf_counter() - started) * 1000),
        )

    async def supports_tools(self, model_config_id: str | None) -> bool:
        if not model_config_id:
            return True
        config = await self.repo.get_config(model_config_id)
        if config is None:
            raise NotFoundError(f"Model config {model_config_id} not found")
        return bool((config.capabilities or {}).get("supports_tools", True))

    # ---------------------------------------------------------------- providers

    async def _provider_out(self, provider: ModelProvider) -> ProviderOut:
        return ProviderOut(
            id=provider.id,
            name=provider.name,
            provider_type=provider.provider_type,
            base_url=provider.base_url,
            enabled=provider.enabled,
            description=provider.description,
            has_api_key=bool(provider.api_key_encrypted),
            masked_api_key="********" if provider.api_key_encrypted else "",
            extra=_mask_extra(provider.extra or {}),
            created_at=provider.created_at,
        )

    async def list_providers(self) -> list[ProviderOut]:
        return [await self._provider_out(item) for item in await self.repo.list_providers()]

    async def create_provider(self, payload: ProviderCreate) -> ProviderOut:
        if await self.repo.get_provider_by_name(payload.name):
            raise ConflictError("Provider name already exists")
        provider = ModelProvider(
            name=payload.name,
            provider_type=payload.provider_type,
            base_url=payload.base_url,
            api_key_encrypted=encrypt_secret(payload.api_key or ""),
            enabled=payload.enabled,
            description=payload.description,
            extra=payload.extra,
        )
        self.db.add(provider)
        await self.db.commit()
        await self.db.refresh(provider)
        return await self._provider_out(provider)

    async def update_provider(self, provider_id: str, payload: ProviderUpdate) -> ProviderOut:
        provider = await self.repo.get_provider(provider_id)
        if provider is None:
            raise NotFoundError("Provider not found")
        if payload.name and payload.name != provider.name:
            if await self.repo.get_provider_by_name(payload.name):
                raise ConflictError("Provider name already exists")
            provider.name = payload.name
        if payload.base_url is not None:
            new_base_url = payload.base_url.rstrip("/") if payload.base_url else None
            if new_base_url != provider.base_url and payload.api_key is None:
                # Never send a stored credential to a newly configured host.
                provider.api_key_encrypted = ""
            provider.base_url = new_base_url
        if payload.api_key is not None:
            provider.api_key_encrypted = encrypt_secret(payload.api_key)
        if payload.enabled is not None:
            provider.enabled = payload.enabled
        if payload.description is not None:
            provider.description = payload.description
        if payload.extra is not None:
            provider.extra = payload.extra
        await self.db.commit()
        return await self._provider_out(provider)

    async def delete_provider(self, provider_id: str) -> None:
        provider = await self.repo.get_provider(provider_id)
        if provider is None:
            raise NotFoundError("Provider not found")
        configs = [c for c in await self.repo.list_configs() if c.provider_id == provider_id]
        if configs:
            raise ConflictError("Provider is referenced by model configs")
        await self.db.delete(provider)
        await self.db.commit()

    # ------------------------------------------------------------------ configs

    async def _config_out(self, config: ModelConfig) -> ModelConfigOut:
        provider = await self.repo.get_provider(config.provider_id)
        return ModelConfigOut(
            id=config.id,
            name=config.name,
            display_name=config.display_name or config.name,
            provider_id=config.provider_id,
            provider_name=provider.name if provider else "unknown",
            provider_type=provider.provider_type if provider else "unknown",
            model_name=config.model_name,
            params=ModelParams(**(config.params or {})),
            capabilities=ModelCapabilities(**(config.capabilities or {})),
            enabled=config.enabled,
            is_default=config.is_default,
            description=config.description,
            extra=_mask_extra(config.extra or {}),
            created_at=config.created_at,
        )

    async def list_configs(self) -> list[ModelConfigOut]:
        return [await self._config_out(item) for item in await self.repo.list_configs()]

    async def get_config(self, config_id: str) -> ModelConfigOut:
        config = await self.repo.get_config(config_id)
        if config is None:
            raise NotFoundError("Model config not found")
        return await self._config_out(config)

    async def create_config(self, payload: ModelConfigCreate) -> ModelConfigOut:
        if await self.repo.get_config_by_name(payload.name):
            raise ConflictError("Model config name already exists")
        provider = await self.repo.get_provider(payload.provider_id)
        if provider is None:
            raise ValidationError("Provider does not exist")
        if payload.is_default:
            await self.repo.clear_defaults()
        config = ModelConfig(
            name=payload.name,
            display_name=payload.display_name or payload.name,
            provider_id=payload.provider_id,
            model_name=payload.model_name,
            params=payload.params.model_dump(),
            capabilities=payload.capabilities.model_dump(),
            extra=payload.extra,
            enabled=payload.enabled,
            is_default=payload.is_default,
            description=payload.description,
        )
        self.db.add(config)
        await self.db.commit()
        await self.db.refresh(config)
        return await self._config_out(config)

    async def update_config(self, config_id: str, payload: ModelConfigUpdate) -> ModelConfigOut:
        config = await self.repo.get_config(config_id)
        if config is None:
            raise NotFoundError("Model config not found")
        if payload.provider_id:
            if await self.repo.get_provider(payload.provider_id) is None:
                raise ValidationError("Provider does not exist")
            config.provider_id = payload.provider_id
        if payload.display_name is not None:
            config.display_name = payload.display_name
        if payload.model_name is not None:
            config.model_name = payload.model_name
        if payload.params is not None:
            config.params = payload.params.model_dump()
        if payload.capabilities is not None:
            config.capabilities = payload.capabilities.model_dump()
        if payload.extra is not None:
            config.extra = payload.extra
        if payload.enabled is not None:
            config.enabled = payload.enabled
        if payload.is_default is not None:
            if payload.is_default:
                await self.repo.clear_defaults(except_id=config.id)
            config.is_default = payload.is_default
        if payload.description is not None:
            config.description = payload.description
        await self.db.commit()
        return await self._config_out(config)

    async def delete_config(self, config_id: str) -> None:
        config = await self.repo.get_config(config_id)
        if config is None:
            raise NotFoundError("Model config not found")
        await self.db.delete(config)
        await self.db.commit()
