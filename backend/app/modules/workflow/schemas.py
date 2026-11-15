from __future__ import annotations

import re
from collections import deque
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

NodeType = Literal[
    "start",
    "llm",
    "agent",
    "tool",
    "knowledge",
    "condition",
    "approval",
    "end",
]

_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")
_OUTPUT_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


class RetryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(default=1, ge=1, le=5)
    backoff_seconds: float = Field(default=0.0, ge=0.0, le=30.0)


class WorkflowNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    type: NodeType
    name: str = Field(default="", max_length=120)
    config: dict[str, Any] = Field(default_factory=dict)
    next: str | None = None
    on_true: str | None = None
    on_false: str | None = None
    retry: RetryPolicy | None = None

    @field_validator("id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        if not _ID_RE.match(value):
            raise ValueError(
                "node id must start with a letter/underscore and contain only "
                "letters, digits, '_' or '-'"
            )
        return value


class WorkflowDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(default=1, ge=1)
    start_node: str | None = None
    nodes: list[WorkflowNode] = Field(min_length=2, max_length=100)
    output: str = ""

    @field_validator("nodes")
    @classmethod
    def _unique_nodes(cls, value: list[WorkflowNode]) -> list[WorkflowNode]:
        ids = [node.id for node in value]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate node ids")
        return value


class WorkflowValidationResult(BaseModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class WorkflowSnapshot(BaseModel):
    workflow_id: str
    workflow_name: str
    owner_id: str
    version_id: str
    version: int
    definition: WorkflowDefinition


class WorkflowCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    description: str = ""
    definition: WorkflowDefinition


class WorkflowUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    description: str | None = None
    definition: WorkflowDefinition | None = None


class WorkflowOut(BaseModel):
    id: str
    name: str
    description: str
    status: str
    owner_id: str
    current_version: int | None = None
    current_version_id: str | None = None
    definition: WorkflowDefinition
    published_at: str | None = None
    created_at: str
    updated_at: str


class WorkflowVersionOut(BaseModel):
    id: str
    workflow_id: str
    version: int
    definition: dict[str, Any]
    status: str
    created_by: str | None = None
    published_at: str | None = None
    created_at: str


class WorkflowExecuteRequest(BaseModel):
    input: str = ""
    variables: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


def _output_key(config: dict[str, Any], default: str) -> str | None:
    value = config.get("output_key", default)
    if value is None:
        return None
    if not isinstance(value, str) or not _OUTPUT_KEY_RE.match(value):
        return None
    return value


def validate_definition(definition: WorkflowDefinition) -> WorkflowValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    nodes = {node.id: node for node in definition.nodes}

    starts = [node for node in definition.nodes if node.type == "start"]
    ends = [node for node in definition.nodes if node.type == "end"]
    if len(starts) != 1:
        errors.append("workflow must contain exactly one start node")
    if len(ends) < 1:
        errors.append("workflow must contain at least one end node")
    if len(ends) > 20:
        errors.append("workflow may contain at most 20 end nodes")
    if starts:
        if definition.start_node and definition.start_node != starts[0].id:
            errors.append("start_node must reference the single start node")
        start = starts[0]
        if start.next is None:
            errors.append("start node requires next")
    elif definition.start_node and definition.start_node not in nodes:
        errors.append(f"start_node references unknown node {definition.start_node}")

    for node in definition.nodes:
        if node.type == "end":
            if node.next or node.on_true or node.on_false:
                errors.append(f"end node {node.id} must not have outgoing transitions")
            continue
        if node.type == "condition":
            if not node.on_true or not node.on_false:
                errors.append(f"condition node {node.id} requires on_true and on_false")
            for target in (node.on_true, node.on_false):
                if target and target not in nodes:
                    errors.append(f"condition node {node.id} references unknown node {target}")
            if node.next:
                warnings.append(f"condition node {node.id} ignores next")
        else:
            if not node.next:
                errors.append(f"node {node.id} requires next")
            elif node.next not in nodes:
                errors.append(f"node {node.id} references unknown node {node.next}")
            if node.on_true or node.on_false:
                warnings.append(f"node {node.id} ignores on_true/on_false")

        config = node.config
        if node.type == "llm":
            if not isinstance(config.get("prompt"), str) or not config["prompt"].strip():
                errors.append(f"llm node {node.id} requires a non-empty prompt")
            if _output_key(config, "llm_output") is None:
                errors.append(f"llm node {node.id} has an invalid output_key")
        elif node.type == "agent":
            if not isinstance(config.get("agent_id"), str) or not config["agent_id"].strip():
                errors.append(f"agent node {node.id} requires agent_id")
            if _output_key(config, "agent_output") is None:
                errors.append(f"agent node {node.id} has an invalid output_key")
        elif node.type == "tool":
            if not isinstance(config.get("tool_name"), str) or not config["tool_name"].strip():
                errors.append(f"tool node {node.id} requires tool_name")
            if not isinstance(config.get("arguments", {}), dict):
                errors.append(f"tool node {node.id} arguments must be an object")
            if _output_key(config, "tool_output") is None:
                errors.append(f"tool node {node.id} has an invalid output_key")
        elif node.type == "knowledge":
            bases = config.get("knowledge_base_ids")
            if not isinstance(bases, list) or not bases:
                errors.append(f"knowledge node {node.id} requires knowledge_base_ids")
            if not isinstance(config.get("query"), str) or not config["query"].strip():
                errors.append(f"knowledge node {node.id} requires a query")
            if _output_key(config, "knowledge") is None:
                errors.append(f"knowledge node {node.id} has an invalid output_key")
        elif node.type == "condition":
            if not isinstance(config.get("expression"), str) or not config["expression"].strip():
                errors.append(f"condition node {node.id} requires an expression")
        elif node.type == "approval":
            on_reject = config.get("on_reject", "fail")
            if on_reject not in {"fail", "continue"}:
                errors.append(f"approval node {node.id} on_reject must be 'fail' or 'continue'")
        elif node.type == "start":
            if config:
                warnings.append(f"start node {node.id} config is ignored")

    if errors:
        return WorkflowValidationResult(valid=False, errors=errors, warnings=warnings)

    # Reachability from start.
    if starts:
        reachable: set[str] = set()
        queue: deque[str] = deque([starts[0].id])
        while queue:
            current = queue.popleft()
            if current in reachable:
                continue
            reachable.add(current)
            node = nodes[current]
            targets = (
                [node.on_true, node.on_false]
                if node.type == "condition"
                else [node.next]
            )
            for target in targets:
                if target and target not in reachable:
                    queue.append(target)
        unreachable = sorted(set(nodes) - reachable)
        if unreachable:
            errors.append("unreachable nodes: " + ", ".join(unreachable))

    # Directed cycle detection.
    color: dict[str, int] = {}

    def visit(node_id: str) -> str | None:
        color[node_id] = 1
        node = nodes[node_id]
        targets = (
            [node.on_true, node.on_false]
            if node.type == "condition"
            else [node.next]
        )
        for target in targets:
            if not target:
                continue
            if color.get(target) == 1:
                return target
            if color.get(target, 0) == 0:
                cycle = visit(target)
                if cycle:
                    return cycle
        color[node_id] = 2
        return None

    if starts:
        cycle = visit(starts[0].id)
        if cycle:
            errors.append(f"workflow contains a cycle involving node {cycle}")

    return WorkflowValidationResult(valid=not errors, errors=errors, warnings=warnings)


__all__ = [
    "NodeType",
    "RetryPolicy",
    "WorkflowCreate",
    "WorkflowDefinition",
    "WorkflowExecuteRequest",
    "WorkflowNode",
    "WorkflowOut",
    "WorkflowSnapshot",
    "WorkflowUpdate",
    "WorkflowValidationResult",
    "WorkflowVersionOut",
    "validate_definition",
]
