from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ProviderCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    provider_type: Literal["mock", "openai_compatible"]
    base_url: str | None = None
    api_key: str | None = None
    enabled: bool = True
    description: str = ""
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("base_url")
    @classmethod
    def _strip_url(cls, value: str | None) -> str | None:
        return value.rstrip("/") if value else value


class ProviderUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    base_url: str | None = None
    api_key: str | None = None
    enabled: bool | None = None
    description: str | None = None
    extra: dict[str, Any] | None = None


class ProviderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    provider_type: str
    base_url: str | None
    enabled: bool
    description: str
    has_api_key: bool = False
    masked_api_key: str = ""
    extra: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class ModelParams(BaseModel):
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_tokens: int = Field(default=2048, ge=1, le=131072)
    top_p: float = Field(default=1.0, gt=0.0, le=1.0)


class ModelCapabilities(BaseModel):
    supports_tools: bool = True
    supports_streaming: bool = True
    supports_json: bool = True


class ModelConfigCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    display_name: str = ""
    provider_id: str
    model_name: str = Field(min_length=1, max_length=200)
    params: ModelParams = Field(default_factory=ModelParams)
    capabilities: ModelCapabilities = Field(default_factory=ModelCapabilities)
    enabled: bool = True
    is_default: bool = False
    description: str = ""
    extra: dict[str, Any] = Field(default_factory=dict)


class ModelConfigUpdate(BaseModel):
    display_name: str | None = None
    provider_id: str | None = None
    model_name: str | None = None
    params: ModelParams | None = None
    capabilities: ModelCapabilities | None = None
    enabled: bool | None = None
    is_default: bool | None = None
    description: str | None = None
    extra: dict[str, Any] | None = None


class ModelConfigOut(BaseModel):
    id: str
    name: str
    display_name: str
    provider_id: str
    provider_name: str
    provider_type: str
    model_name: str
    params: ModelParams
    capabilities: ModelCapabilities
    enabled: bool
    is_default: bool
    description: str
    extra: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class ModelHealthOut(BaseModel):
    ok: bool
    message: str
    latency_ms: int = 0
