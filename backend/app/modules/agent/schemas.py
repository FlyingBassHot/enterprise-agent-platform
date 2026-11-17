from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AgentModelBinding(BaseModel):
    config_id: str
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_tokens: int = Field(default=2048, ge=1, le=131072)
    top_p: float = Field(default=1.0, gt=0.0, le=1.0)


class PromptBinding(BaseModel):
    prompt_id: str
    version_id: str | None = None
    variables: dict[str, str] = Field(default_factory=dict)


class VerificationSettings(BaseModel):
    enabled: bool = True
    require_plan_completion: bool = True
    require_citations: bool = False
    max_retries: int = Field(default=1, ge=0, le=2)


class ExecutionSettings(BaseModel):
    mode: Literal["agent", "workflow", "hybrid"] = "agent"
    workflow_id: str | None = None
    max_iterations: int = Field(default=12, ge=1, le=200)
    timeout_seconds: int = Field(default=600, ge=1, le=86400)
    approval_risk_levels: list[Literal["low", "medium", "high", "critical"]] = Field(
        default_factory=lambda: ["high", "critical"]
    )
    verification: VerificationSettings = Field(default_factory=VerificationSettings)
    # Optional per-agent cap; the platform setting is an upper bound.
    token_budget: int | None = Field(default=None, ge=0)
    streaming: bool = True


class PermissionSettings(BaseModel):
    required_permissions: list[str] = Field(default_factory=list)
    allowed_roles: list[str] = Field(default_factory=list)


class RetrievalSettings(BaseModel):
    enabled: bool = True
    top_k: int = Field(default=5, ge=1, le=20)


class AgentConfig(BaseModel):
    model: AgentModelBinding
    prompt: PromptBinding
    tools: list[str] = Field(default_factory=list)
    knowledge_base_ids: list[str] = Field(default_factory=list)
    skill_names: list[str] = Field(default_factory=list)
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)
    execution: ExecutionSettings = Field(default_factory=ExecutionSettings)
    permissions: PermissionSettings = Field(default_factory=PermissionSettings)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("tools", "knowledge_base_ids", "skill_names")
    @classmethod
    def _unique_bindings(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))


class AgentCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    description: str = ""
    config: AgentConfig


class AgentUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    description: str | None = None
    config: AgentConfig | None = None


class AgentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str
    status: str
    owner_id: str
    draft_config: AgentConfig
    current_version_id: str | None = None
    current_version: int | None = None
    published_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class AgentVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    agent_id: str
    version: int
    config: dict[str, Any]
    snapshot: dict[str, Any]
    status: str
    published_by: str | None = None
    published_at: datetime | None = None
    created_at: datetime


class AgentValidationResult(BaseModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
