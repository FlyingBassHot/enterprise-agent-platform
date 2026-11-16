"""Cross-module contracts.

Modules must depend on these DTOs/protocols instead of importing each other's
ORM models. Protocols are implemented structurally by the owning services.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Chat / model
# ---------------------------------------------------------------------------


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)

    @property
    def arguments_json(self) -> str:
        import json

        return json.dumps(self.arguments, ensure_ascii=False, default=str)


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None

    def to_provider(self) -> dict[str, Any]:
        data: dict[str, Any] = {"role": self.role, "content": self.content or ""}
        if self.tool_calls:
            data["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": call.arguments_json,
                    },
                }
                for call in self.tool_calls
            ]
            data["content"] = self.content or None
        if self.tool_call_id:
            data["tool_call_id"] = self.tool_call_id
        if self.name:
            data["name"] = self.name
        return data


class ToolSpec(BaseModel):
    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)

    def to_provider(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema or {"type": "object", "properties": {}},
            },
        }


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatResult(BaseModel):
    message: ChatMessage
    finish_reason: str = "stop"
    usage: Usage = Field(default_factory=Usage)


class ModelStreamEvent(BaseModel):
    """Granular output of a streaming completion.

    kind=delta      -> content chunk
    kind=tool_call  -> a complete tool call (providers may emit at the end)
    kind=done       -> finish_reason + usage
    """

    kind: Literal["delta", "tool_call", "done"]
    content: str = ""
    tool_call: ToolCall | None = None
    finish_reason: str = "stop"
    usage: Usage = Field(default_factory=Usage)


class ModelConfigResolver(BaseModel):
    """Resolved runtime model settings (secrets never cross this boundary)."""

    config_id: str | None = None
    provider_type: str = "mock"
    provider_name: str = "mock"
    model: str = "mock-agent"
    base_url: str | None = None
    temperature: float = 0.2
    max_tokens: int = 2048
    top_p: float = 1.0
    extra: dict[str, Any] = Field(default_factory=dict)


class ModelGateway(Protocol):
    async def chat(
        self,
        *,
        model_config_id: str | None,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec] = (),
        temperature: float | None = None,
        max_tokens: int | None = None,
        execution_id: str | None = None,
    ) -> ChatResult: ...

    def stream_chat(
        self,
        *,
        model_config_id: str | None,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec] = (),
        temperature: float | None = None,
        max_tokens: int | None = None,
        execution_id: str | None = None,
    ) -> AsyncIterator[ModelStreamEvent]: ...

    async def health(self, model_config_id: str) -> tuple[bool, str]: ...

    async def resolve(self, model_config_id: str | None) -> ModelConfigResolver: ...


# ---------------------------------------------------------------------------
# Identity / permission
# ---------------------------------------------------------------------------


class UserContext(BaseModel):
    user_id: str
    username: str
    display_name: str = ""
    is_superuser: bool = False
    roles: list[str] = Field(default_factory=list)
    permissions: set[str] = Field(default_factory=set)
    department_id: str | None = None
    tenant_id: str = "default"

    def has(self, permission: str) -> bool:
        return self.is_superuser or permission in self.permissions or "*" in self.permissions


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class AgentModelSettings(BaseModel):
    config_id: str | None = None
    temperature: float = 0.2
    max_tokens: int = 2048
    top_p: float = 1.0


class AgentSnapshot(BaseModel):
    """Immutable published configuration used by the runtime."""

    agent_id: str
    agent_name: str
    version_id: str
    version: int
    owner_id: str
    mode: str = "agent"
    model: AgentModelSettings = Field(default_factory=AgentModelSettings)
    system_prompt: str = "You are a helpful enterprise assistant."
    task_prompt: str = ""
    tool_instructions: str = ""
    prompt_variables: dict[str, str] = Field(default_factory=dict)
    tool_names: list[str] = Field(default_factory=list)
    tool_versions: dict[str, str] = Field(default_factory=dict)
    skill_names: list[str] = Field(default_factory=list)
    knowledge_base_ids: list[str] = Field(default_factory=list)
    retrieval_top_k: int = 5
    retrieval_enabled: bool = True
    max_iterations: int = 12
    timeout_seconds: int = 600
    approval_risk_levels: list[str] = Field(default_factory=lambda: ["high", "critical"])
    verification_enabled: bool = True
    verification_require_plan_completion: bool = True
    verification_require_citations: bool = False
    verification_max_retries: int = 1
    workflow_id: str | None = None
    workflow_version_id: str | None = None
    required_permissions: list[str] = Field(default_factory=list)
    allowed_roles: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentLoader(Protocol):
    async def load_published(self, agent_id: str) -> AgentSnapshot: ...


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


class ToolDescriptor(BaseModel):
    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    version: str = "1"
    source_type: str = "native"
    risk_level: str = "low"
    timeout_seconds: int = 60
    permission_scope: list[str] = Field(default_factory=list)
    requires_approval: bool = False
    enabled: bool = True
    sensitive_fields: list[str] = Field(default_factory=list)

    def to_spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description=self.description, input_schema=self.input_schema)


class ToolInvocation(BaseModel):
    execution_id: str | None = None
    tool_call_id: str | None = None
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    user_id: str
    agent_id: str | None = None
    approved: bool = False


class ToolContext(BaseModel):
    """Passed to native tool handlers; no ORM objects cross this boundary."""

    user: UserContext
    tool_name: str
    execution_id: str | None = None
    agent_id: str | None = None


class ArtifactInfo(BaseModel):
    id: str
    filename: str
    content_type: str
    size: int
    storage_key: str
    owner_id: str
    created_at: str | None = None



class ToolExecutionResult(BaseModel):
    ok: bool
    status: str = "success"
    output: Any = None
    error: str | None = None
    duration_ms: int = 0
    masked_arguments: dict[str, Any] = Field(default_factory=dict)
    replayed: bool = False


class ToolGateway(Protocol):
    async def describe(
        self, names: Sequence[str], user: UserContext
    ) -> list[ToolDescriptor]: ...

    async def execute(
        self, invocation: ToolInvocation, user: UserContext
    ) -> ToolExecutionResult: ...


class VerificationIssue(BaseModel):
    code: str
    message: str
    step_ids: list[str] = Field(default_factory=list)


class VerificationOutcome(BaseModel):
    """Deterministic verdict on a completion claim."""

    status: Literal["passed", "failed"]
    issues: list[VerificationIssue] = Field(default_factory=list)
    next_action: Literal[
        "complete", "retry", "replan", "request_input", "request_approval", "fail"
    ] = "complete"


class EnvironmentResource(BaseModel):
    id: str
    name: str
    kind: str = "artifact"
    content_type: str = ""
    size: int = 0
    access: str = "read"


class EnvironmentDescriptor(BaseModel):
    """What the agent works with, declared before the first model call."""

    kind: str
    environment_id: str
    label: str = ""
    resources: list[EnvironmentResource] = Field(default_factory=list)
    constraints: dict[str, Any] = Field(default_factory=dict)


class EnvironmentGateway(Protocol):
    async def open(
        self, *, execution_id: str, user: UserContext
    ) -> EnvironmentDescriptor: ...

    async def close(self, environment_id: str) -> None: ...


class VerifierGateway(Protocol):
    async def verify(
        self,
        *,
        execution_id: str,
        snapshot: AgentSnapshot,
        plan: Sequence[dict[str, str]],
        output_text: str,
        citations: Sequence[dict[str, Any]],
    ) -> VerificationOutcome: ...


class TaskGateway(Protocol):
    """Task-scoped plan state that outlives a single execution."""

    async def steps_for_execution(self, execution_id: str) -> list[dict[str, str]]: ...

    async def apply_plan(
        self, execution_id: str, plan: Sequence[dict[str, str]]
    ) -> None: ...


# ---------------------------------------------------------------------------
# Knowledge / skills
# ---------------------------------------------------------------------------


class RetrievalChunk(BaseModel):
    chunk_id: str
    document_id: str
    document_title: str
    knowledge_base_id: str
    content: str
    score: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class KnowledgeGateway(Protocol):
    async def search(
        self,
        *,
        knowledge_base_ids: Sequence[str],
        query: str,
        user: UserContext,
        top_k: int = 5,
        metadata_filter: dict[str, Any] | None = None,
    ) -> list[RetrievalChunk]: ...


class SkillContext(BaseModel):
    name: str
    version: str
    instructions: str
    required_tools: list[str] = Field(default_factory=list)
    required_permissions: list[str] = Field(default_factory=list)


class SkillGateway(Protocol):
    async def resolve_for_agent(
        self, skill_names: Sequence[str], user: UserContext
    ) -> list[SkillContext]: ...


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


class ExecutionSpec(BaseModel):
    agent_id: str
    user_id: str
    input_text: str
    conversation_id: str | None = None
    variables: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExecutionHandle(BaseModel):
    execution_id: str
    conversation_id: str
    status: str


class RuntimeEngine(Protocol):
    async def start(self, spec: ExecutionSpec, user: UserContext) -> ExecutionHandle: ...

    async def resume(self, execution_id: str, user: UserContext) -> ExecutionHandle: ...

    async def cancel(self, execution_id: str, user: UserContext) -> None: ...


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------


class WorkflowInvocation(BaseModel):
    workflow_id: str
    workflow_version: int
    execution_id: str
    user_id: str
    input: dict[str, Any] = Field(default_factory=dict)
    agent_id: str | None = None


class WorkflowGateway(Protocol):
    async def run(self, invocation: WorkflowInvocation) -> dict[str, Any]: ...
