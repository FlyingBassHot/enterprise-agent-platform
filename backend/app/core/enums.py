from __future__ import annotations

from enum import StrEnum


class AgentStatus(StrEnum):
    DRAFT = "draft"
    VALIDATING = "validating"
    PUBLISHED = "published"
    DISABLED = "disabled"
    ARCHIVED = "archived"


class ExecutionMode(StrEnum):
    WORKFLOW = "workflow"
    AGENT = "agent"
    HYBRID = "hybrid"


class ExecutionStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    # Stopped at the iteration budget with a usable partial answer; resumable.
    INCOMPLETE = "incomplete"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ApprovalKind(StrEnum):
    TOOL_CALL = "tool_call"
    WORKFLOW_NODE = "workflow_node"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def weight(self) -> int:
        return {"low": 1, "medium": 2, "high": 3, "critical": 4}[self.value]


class ToolSourceType(StrEnum):
    NATIVE = "native"
    HTTP = "http"
    MCP = "mcp"


class ToolExecutionStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    DENIED = "denied"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


class ModelProviderType(StrEnum):
    MOCK = "mock"
    OPENAI_COMPATIBLE = "openai_compatible"


class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class KnowledgeDocumentStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"
    DELETED = "deleted"


class SkillStatus(StrEnum):
    INSTALLED = "installed"
    ENABLED = "enabled"
    DISABLED = "disabled"


class WorkflowStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    DISABLED = "disabled"


class WorkflowExecutionStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_EXECUTION_STATUSES = {
    ExecutionStatus.COMPLETED,
    ExecutionStatus.INCOMPLETE,
    ExecutionStatus.FAILED,
    ExecutionStatus.CANCELLED,
}
