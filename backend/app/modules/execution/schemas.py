from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Conversations / messages
# ---------------------------------------------------------------------------


class ConversationCreate(BaseModel):
    agent_id: str
    title: str = ""


class ConversationOut(_ORMModel):
    id: str
    agent_id: str
    user_id: str
    title: str
    created_at: datetime
    updated_at: datetime


class MessageOut(_ORMModel):
    id: str
    conversation_id: str
    role: str
    content: str
    tool_calls: list[Any] | None = None
    execution_id: str | None = None
    created_at: datetime


# ---------------------------------------------------------------------------
# Executions
# ---------------------------------------------------------------------------


class ExecutionCreate(BaseModel):
    agent_id: str | None = None
    workflow_id: str | None = None
    input: str = ""
    conversation_id: str | None = None
    artifact_ids: list[str] = Field(default_factory=list)
    variables: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _require_target(self) -> ExecutionCreate:
        if not self.agent_id and not self.workflow_id:
            raise ValueError("agent_id or workflow_id is required")
        return self


class ExecutionOut(_ORMModel):
    id: str
    agent_id: str
    agent_version_id: str
    agent_version: int
    workflow_id: str | None = None
    workflow_version_id: str | None = None
    user_id: str
    conversation_id: str | None
    mode: str
    status: str
    input_text: str
    output_text: str | None = None
    error: str | None = None
    trace_id: str | None = None
    token_usage: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ExecutionEventOut(_ORMModel):
    seq: int
    execution_id: str
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class LLMCallOut(_ORMModel):
    id: str
    execution_id: str
    model_config_id: str | None = None
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    latency_ms: int
    status: str
    error: str | None = None
    created_at: datetime


class ApprovalOut(_ORMModel):
    id: str
    execution_id: str
    kind: str
    tool_call_id: str | None = None
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    status: str
    requested_at: datetime
    resolved_at: datetime | None = None
    resolved_by: str | None = None
    comment: str | None = None


class ExecutionDetailOut(ExecutionOut):
    input_payload: dict[str, Any] = Field(default_factory=dict)
    output_payload: dict[str, Any] | None = None
    events_count: int = 0
    approvals: list[ApprovalOut] = Field(default_factory=list)
    llm_calls: list[LLMCallOut] = Field(default_factory=list)


class ResumeExecutionRequest(BaseModel):
    """Optional extra budget for resuming an exhausted execution."""

    extra_iterations: int | None = Field(default=None, ge=1, le=100)


class ApprovalDecision(BaseModel):
    comment: str = ""


class CancelResult(BaseModel):
    execution_id: str
    status: str


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------


class ArtifactOut(_ORMModel):
    id: str
    filename: str
    content_type: str
    size: int
    storage_key: str
    owner_id: str
    conversation_id: str | None = None
    created_at: datetime


__all__ = [
    "ApprovalDecision",
    "ApprovalOut",
    "ArtifactOut",
    "CancelResult",
    "ConversationCreate",
    "ConversationOut",
    "ExecutionCreate",
    "ExecutionDetailOut",
    "ExecutionEventOut",
    "ExecutionOut",
    "LLMCallOut",
    "MessageOut",
    "ResumeExecutionRequest",
]
