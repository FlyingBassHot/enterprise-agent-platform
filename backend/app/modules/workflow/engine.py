"""LangGraph wiring for frozen workflow definitions.

The graph is rebuilt for every run so node functions can close over the user
context, gateways and execution sink.  Durable state (including approval
interrupts) lives in the checkpointer, keyed by ``thread_id=execution_id``.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypedDict

from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.core.config import get_settings
from app.core.contracts import (
    AgentLoader,
    AgentSnapshot,
    ChatMessage,
    KnowledgeGateway,
    ModelGateway,
    SkillGateway,
    ToolGateway,
    ToolInvocation,
    UserContext,
)
from app.modules.runtime.context import (
    ExecutionSink,
    LLMCallRecord,
    accumulate_usage,
    build_system_prompt,
    mask_arguments,
    truncate_tool_output,
)
from app.modules.workflow.schemas import WorkflowNode, WorkflowSnapshot

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph

_APPROVAL_RISK_LEVELS = frozenset({"high", "critical"})
_TEMPLATE_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_.-]*)\s*\}\}")
_OUTPUT_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_CONDITION_RE = re.compile(
    r"^\s*(?P<neg>not\s+)?"
    r"(?P<field>[A-Za-z_][A-Za-z0-9_.-]*)"
    r"\s*(?P<op>==|!=|>=|<=|>|<|contains)\s*"
    r"(?P<literal>.+?)\s*$"
)
_MISSING = object()


class WorkflowState(TypedDict, total=False):
    execution_id: str
    variables: dict[str, Any]
    steps: list[dict[str, Any]]
    usage: dict[str, int]
    final_output: str | None
    error: str | None
    last_condition: bool | None
    citations: list[dict[str, Any]]
    node_outputs: dict[str, Any]
    last_step_output: Any


@dataclass(slots=True)
class _Outcome:
    output: Any = None
    variables: dict[str, Any] | None = None
    usage: dict[str, int] | None = None
    citations: list[dict[str, Any]] | None = None
    last_condition: bool | None = None
    final_output: str | None = None
    status: str = "success"


# ---------------------------------------------------------------------------
# Template rendering / JSON helpers
# ---------------------------------------------------------------------------


def _lookup(path: str, variables: dict[str, Any]) -> Any:
    current: Any = variables
    for part in path.split("."):
        if isinstance(current, dict):
            if part not in current:
                return _MISSING
            current = current[part]
        elif isinstance(current, (list, tuple)) and part.isdigit():
            index = int(part)
            if index >= len(current):
                return _MISSING
            current = current[index]
        else:
            return _MISSING
    return current


def render_text(template: str, variables: dict[str, Any] | None = None) -> str:
    """Render ``{{ dotted.path }}`` placeholders; unknown paths stay verbatim."""

    if not template:
        return ""
    values = variables or {}

    def replace(match: re.Match[str]) -> str:
        value = _lookup(match.group(1), values)
        if value is _MISSING:
            return match.group(0)
        if isinstance(value, str):
            return value
        return json.dumps(json_safe(value), ensure_ascii=False, default=str)

    return _TEMPLATE_RE.sub(replace, template)


def render_value(value: Any, variables: dict[str, Any] | None = None) -> Any:
    """Recursively render templates inside tool arguments and filters."""

    if isinstance(value, str):
        return render_text(value, variables)
    if isinstance(value, dict):
        return {key: render_value(item, variables) for key, item in value.items()}
    if isinstance(value, list):
        return [render_value(item, variables) for item in value]
    return value


def json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]
    return str(value)


def _preview(value: Any, limit: int = 400) -> str:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(json_safe(value), ensure_ascii=False, default=str)
    return text[:limit]


def _output_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(json_safe(value), ensure_ascii=False, default=str)


def _output_key(config: dict[str, Any], default: str) -> str:
    value = config.get("output_key", default)
    if isinstance(value, str) and _OUTPUT_KEY_RE.match(value):
        return value
    return default


def _normalize_decision(raw: Any) -> dict[str, Any]:
    if isinstance(raw, bool):
        return {"approved": raw, "comment": ""}
    if isinstance(raw, dict):
        approved = raw.get("approved")
        if isinstance(approved, str):
            approved = approved.strip().lower() in {"1", "true", "yes", "approved"}
        return {"approved": bool(approved), "comment": str(raw.get("comment") or "")}
    return {"approved": False, "comment": ""}


# ---------------------------------------------------------------------------
# Safe condition evaluation
# ---------------------------------------------------------------------------


def _parse_literal(raw: str) -> Any:
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        if text[0] == '"':
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text[1:-1]
        return text[1:-1].replace("\\'", "'").replace("\\\\", "\\")
    lowered = text.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "none"}:
        return None
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?", text) and "." in text:
        return float(text)
    raise ValueError(f"unsupported condition literal {raw!r}; quote string literals")


def _compare(actual: Any, op: str, expected: Any) -> bool:
    if actual is _MISSING:
        return False
    if op == "==":
        return actual == expected
    if op == "!=":
        return actual != expected
    if op == "contains":
        if isinstance(actual, str):
            return str(expected) in actual
        if isinstance(actual, (list, tuple, set)):
            return expected in actual
        if isinstance(actual, dict):
            return expected in actual
        return False
    if isinstance(actual, bool) or isinstance(expected, bool):
        raise ValueError(f"operator {op!r} requires comparable values")
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        left: Any = actual
        right: Any = expected
    elif isinstance(actual, str) and isinstance(expected, str):
        left, right = actual, expected
    else:
        raise ValueError(
            f"operator {op!r} cannot compare {type(actual).__name__} with {type(expected).__name__}"
        )
    if op == ">":
        return left > right
    if op == ">=":
        return left >= right
    if op == "<":
        return left < right
    return left <= right


def evaluate_condition(expression: str, variables: dict[str, Any]) -> bool:
    """Evaluate ``field op literal`` (optionally prefixed with ``not``)."""

    match = _CONDITION_RE.match(expression or "")
    if not match:
        raise ValueError(
            "unsupported condition expression; expected 'field op literal' "
            "with op in ==, !=, >, >=, <, <=, contains"
        )
    field = match.group("field")
    op = match.group("op")
    expected = _parse_literal(match.group("literal"))
    result = _compare(_lookup(field, variables), op, expected)
    return not result if match.group("neg") else result


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------


class _WorkflowExecutor:
    def __init__(
        self,
        *,
        snapshot: WorkflowSnapshot,
        model_gateway: ModelGateway,
        tool_gateway: ToolGateway,
        knowledge_gateway: KnowledgeGateway | None,
        skill_gateway: SkillGateway | None,
        agent_loader: AgentLoader | None,
        sink: ExecutionSink,
        user: UserContext,
        execution_id: str,
    ) -> None:
        self.snapshot = snapshot
        self.definition = snapshot.definition
        self.model_gateway = model_gateway
        self.tool_gateway = tool_gateway
        self.knowledge_gateway = knowledge_gateway
        self.skill_gateway = skill_gateway
        self.agent_loader = agent_loader
        self.sink = sink
        self.user = user
        self.execution_id = execution_id

    # -- helpers ---------------------------------------------------------

    async def _with_retry(self, node: WorkflowNode, operation: Callable[[], Awaitable[Any]]) -> Any:
        policy = node.retry
        attempts = policy.max_attempts if policy else 1
        backoff = policy.backoff_seconds if policy else 0.0
        for attempt in range(1, attempts + 1):
            try:
                return await operation()
            except (GraphBubbleUp, asyncio.CancelledError):
                raise
            except Exception:
                if attempt >= attempts:
                    raise
                if backoff:
                    await asyncio.sleep(backoff)
        raise RuntimeError("retry loop exhausted")  # pragma: no cover - defensive

    def _node_timeout(self, node: WorkflowNode) -> float | None:
        raw = node.config.get("timeout_seconds")
        if raw is None:
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    async def _chat(
        self,
        *,
        node: WorkflowNode,
        model_config_id: str | None,
        messages: Sequence[ChatMessage],
        tools: Sequence[Any] = (),
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout_seconds: float | None = None,
    ) -> Any:
        provider = "unknown"
        model_name = "unknown"
        resolved_id = model_config_id
        try:
            resolved = await self.model_gateway.resolve(model_config_id)
            provider = resolved.provider_name or resolved.provider_type
            model_name = resolved.model
            resolved_id = resolved.config_id or model_config_id
        except (GraphBubbleUp, asyncio.CancelledError):
            raise
        except Exception:  # noqa: BLE001 - surfaced by the model call below
            pass

        started = time.perf_counter()
        try:
            async with asyncio.timeout(timeout_seconds):
                result = await self.model_gateway.chat(
                    model_config_id=model_config_id,
                    messages=messages,
                    tools=tools,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    execution_id=self.execution_id,
                )
        except (GraphBubbleUp, asyncio.CancelledError):
            raise
        except Exception as exc:
            await self.sink.record_llm_call(
                LLMCallRecord(
                    model_config_id=resolved_id,
                    provider=provider,
                    model=model_name,
                    prompt_tokens=0,
                    completion_tokens=0,
                    total_tokens=0,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    status="failed",
                    error=str(exc),
                )
            )
            raise
        usage = result.usage
        await self.sink.record_llm_call(
            LLMCallRecord(
                model_config_id=resolved_id,
                provider=provider,
                model=model_name,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                total_tokens=usage.total_tokens,
                latency_ms=int((time.perf_counter() - started) * 1000),
                status="success",
            )
        )
        return result

    # -- node implementations -------------------------------------------

    async def _start(self, node: WorkflowNode, state: WorkflowState) -> _Outcome:
        return _Outcome(output=state.get("variables", {}))

    async def _llm(self, node: WorkflowNode, state: WorkflowState) -> _Outcome:
        config = node.config
        variables = dict(state.get("variables") or {})
        prompt = render_text(str(config.get("prompt") or ""), variables)
        messages: list[ChatMessage] = []
        system_prompt = config.get("system_prompt")
        if isinstance(system_prompt, str) and system_prompt.strip():
            messages.append(
                ChatMessage(role="system", content=render_text(system_prompt, variables))
            )
        messages.append(ChatMessage(role="user", content=prompt))
        model_config_id = config.get("model_config_id")
        temperature = config.get("temperature")
        max_tokens = config.get("max_tokens")
        timeout_seconds = self._node_timeout(node)
        result = await self._with_retry(
            node,
            lambda: self._chat(
                node=node,
                model_config_id=model_config_id if isinstance(model_config_id, str) else None,
                messages=messages,
                temperature=float(temperature) if isinstance(temperature, (int, float)) else None,
                max_tokens=int(max_tokens) if isinstance(max_tokens, int) else None,
                timeout_seconds=timeout_seconds,
            ),
        )
        text = result.message.content or ""
        await self.sink.emit(
            "message_completed",
            {"role": "assistant", "content": text, "node_id": node.id, "finish_reason": "stop"},
        )
        key = _output_key(config, "llm_output")
        usage = accumulate_usage(
            state.get("usage", {}),
            result.usage.prompt_tokens,
            result.usage.completion_tokens,
            result.usage.total_tokens,
        )
        return _Outcome(output=text, variables={key: text}, usage=usage)

    async def _agent(self, node: WorkflowNode, state: WorkflowState) -> _Outcome:
        if self.agent_loader is None:
            raise RuntimeError("agent node requires an agent loader")
        config = node.config
        variables = dict(state.get("variables") or {})
        agent_id = str(config.get("agent_id") or "")
        version_id = str(config.get("agent_version_id") or "")
        loader = self.agent_loader
        if version_id and callable(getattr(loader, "load_version", None)):
            snapshot: AgentSnapshot = await self._with_retry(
                node, lambda: loader.load_version(agent_id, version_id)  # type: ignore[union-attr]
            )
        else:
            snapshot = await self._with_retry(
                node, lambda: loader.load_published(agent_id)  # type: ignore[union-attr]
            )
        if snapshot.required_permissions:
            missing = [
                code for code in snapshot.required_permissions if not self.user.has(code)
            ]
            if missing:
                raise PermissionError(
                    f"agent node {node.id} requires permissions: {', '.join(missing)}"
                )
        if snapshot.allowed_roles and not self.user.is_superuser:
            if not set(snapshot.allowed_roles).intersection(self.user.roles):
                raise PermissionError(
                    f"agent node {node.id} role is not allowed to run the agent"
                )
        descriptors = []
        if snapshot.tool_names:
            descriptors = await self._with_retry(
                node, lambda: self.tool_gateway.describe(snapshot.tool_names, self.user)
            )
        approval_levels = _APPROVAL_RISK_LEVELS | set(snapshot.approval_risk_levels)
        blocked = [
            descriptor
            for descriptor in descriptors
            if descriptor.requires_approval or descriptor.risk_level in approval_levels
        ]
        if blocked:
            names = ", ".join(sorted(descriptor.name for descriptor in blocked))
            raise PermissionError(
                f"agent node {node.id} binds tools that require approval: {names}"
            )
        tools = [descriptor.to_spec() for descriptor in descriptors if descriptor.enabled]
        descriptor_by_name = {descriptor.name: descriptor for descriptor in descriptors}

        input_text = render_text(str(config.get("input") or ""), variables)
        skills = []
        if snapshot.skill_names and self.skill_gateway is not None:
            skills = await self.skill_gateway.resolve_for_agent(
                snapshot.skill_names, self.user
            )
        chunks = []
        if (
            snapshot.retrieval_enabled
            and snapshot.knowledge_base_ids
            and self.knowledge_gateway is not None
        ):
            chunks = await self.knowledge_gateway.search(
                knowledge_base_ids=snapshot.knowledge_base_ids,
                query=input_text,
                user=self.user,
                top_k=snapshot.retrieval_top_k,
            )
            if chunks:
                await self.sink.emit(
                    "knowledge_retrieved",
                    {
                        "node_id": node.id,
                        "count": len(chunks),
                        "citations": [
                            {
                                "chunk_id": chunk.chunk_id,
                                "document_id": chunk.document_id,
                                "document_title": chunk.document_title,
                                "score": chunk.score,
                            }
                            for chunk in chunks
                        ],
                    },
                )
        system_prompt = build_system_prompt(
            snapshot,
            variables=variables,
            skills=skills,
            knowledge_chunks=chunks,
            knowledge_context_chars=get_settings().knowledge_context_chars,
        )
        messages: list[ChatMessage] = []
        if system_prompt:
            messages.append(ChatMessage(role="system", content=system_prompt))
        messages.append(ChatMessage(role="user", content=input_text))

        max_iterations = int(config.get("max_iterations") or 4)
        max_iterations = max(1, min(max_iterations, 20))
        timeout_seconds = self._node_timeout(node) or float(snapshot.timeout_seconds or 0) or None
        usage = dict(state.get("usage") or {})
        final_text: str | None = None

        for _ in range(max_iterations):
            result = await self._with_retry(
                node,
                lambda: self._chat(
                    node=node,
                    model_config_id=snapshot.model.config_id,
                    messages=messages,
                    tools=tools,
                    temperature=snapshot.model.temperature,
                    max_tokens=snapshot.model.max_tokens,
                    timeout_seconds=timeout_seconds,
                ),
            )
            usage = accumulate_usage(
                usage,
                result.usage.prompt_tokens,
                result.usage.completion_tokens,
                result.usage.total_tokens,
            )
            assistant = result.message
            text = assistant.content or ""
            messages.append(assistant)
            await self.sink.emit(
                "message_completed",
                {
                    "role": "assistant",
                    "content": text,
                    "tool_calls": [call.model_dump() for call in assistant.tool_calls],
                    "node_id": node.id,
                    "finish_reason": "tool_calls" if assistant.tool_calls else "stop",
                },
            )
            if not assistant.tool_calls:
                final_text = text
                break
            for call in assistant.tool_calls:
                call_descriptor = descriptor_by_name.get(call.name)
                await self.sink.emit(
                    "tool_call_started",
                    {
                        "tool_call_id": call.id,
                        "tool_name": call.name,
                        "arguments": mask_arguments(
                            call.arguments,
                            getattr(call_descriptor, "sensitive_fields", ()),
                        ),
                        "node_id": node.id,
                    },
                )
                started = time.perf_counter()
                status = "success"
                ok = False
                output: Any = None
                error: str | None = None
                try:
                    result_tool = await self.tool_gateway.execute(
                        ToolInvocation(
                            execution_id=self.execution_id,
                            tool_call_id=call.id,
                            tool_name=call.name,
                            arguments=call.arguments,
                            user_id=self.user.user_id,
                            agent_id=snapshot.agent_id,
                            approved=False,
                        ),
                        self.user,
                    )
                    status = result_tool.status
                    ok = result_tool.ok
                    output = result_tool.output
                    error = result_tool.error
                except (GraphBubbleUp, asyncio.CancelledError):
                    raise
                except Exception as exc:  # noqa: BLE001 - tool failures go back to the model
                    status = "failed"
                    error = str(exc)
                duration_ms = int((time.perf_counter() - started) * 1000)
                await self.sink.emit(
                    "tool_call_completed",
                    {
                        "tool_call_id": call.id,
                        "tool_name": call.name,
                        "status": status,
                        "ok": ok,
                        "duration_ms": duration_ms,
                        "error": error,
                        "node_id": node.id,
                    },
                )
                content = json.dumps(
                    {"status": status, "ok": ok, "output": output, "error": error},
                    ensure_ascii=False,
                    default=str,
                )
                messages.append(
                    ChatMessage(
                        role="tool",
                        tool_call_id=call.id,
                        name=call.name,
                        content=truncate_tool_output(content),
                    )
                )
        if final_text is None:
            raise RuntimeError(
                f"agent node {node.id} exceeded max_iterations ({max_iterations}) "
                "without a final answer"
            )
        key = _output_key(config, "agent_output")
        return _Outcome(output=final_text, variables={key: final_text}, usage=usage)

    async def _tool(self, node: WorkflowNode, state: WorkflowState) -> _Outcome:
        config = node.config
        variables = dict(state.get("variables") or {})
        tool_name = str(config.get("tool_name") or "")
        descriptors = await self._with_retry(
            node, lambda: self.tool_gateway.describe([tool_name], self.user)
        )
        descriptor = next(
            (item for item in descriptors if item.name == tool_name and item.enabled), None
        )
        if descriptor is None:
            raise RuntimeError(f"tool {tool_name!r} is unknown or disabled")
        arguments = render_value(config.get("arguments") or {}, variables)
        if not isinstance(arguments, dict):
            arguments = {"value": arguments}

        needs_approval = bool(
            config.get("requires_approval")
            or descriptor.requires_approval
            or descriptor.risk_level in _APPROVAL_RISK_LEVELS
        )
        tool_call_id = f"node:{node.id}"
        if needs_approval:
            safe_arguments = mask_arguments(arguments, descriptor.sensitive_fields)
            title = str(config.get("title") or node.name or tool_name)
            handle = await self.sink.request_approval(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                arguments=safe_arguments,
                risk_level=descriptor.risk_level,
                kind="tool_call",
                title=title,
                approval_key=f"node:{node.id}",
            )
            if handle.created:
                await self.sink.emit(
                    "approval_required",
                    {
                        "approval_id": handle.id,
                        "tool_call_id": tool_call_id,
                        "tool_name": tool_name,
                        "arguments": safe_arguments,
                        "risk_level": descriptor.risk_level,
                        "node_id": node.id,
                        "title": title,
                    },
                )
            raw_decision = interrupt(
                {
                    "approval_id": handle.id,
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "arguments": safe_arguments,
                    "risk_level": descriptor.risk_level,
                    "node_id": node.id,
                }
            )
            decision = _normalize_decision(raw_decision)
            await self.sink.emit(
                "approval_resolved",
                {
                    "approval_id": handle.id,
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "approved": decision["approved"],
                    "comment": decision["comment"],
                    "node_id": node.id,
                },
            )
            if not decision["approved"]:
                key = _output_key(config, "tool_output")
                rejected = {
                    "status": "rejected",
                    "ok": False,
                    "comment": decision["comment"],
                }
                if str(config.get("on_reject") or "fail") == "continue":
                    return _Outcome(
                        output=rejected,
                        variables={key: rejected},
                        status="rejected",
                    )
                raise PermissionError(
                    f"tool {tool_name!r} was rejected by the user"
                    + (f": {decision['comment']}" if decision["comment"] else "")
                )
        else:
            decision = {"approved": True, "comment": ""}

        await self.sink.emit(
            "tool_call_started",
            {
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "arguments": mask_arguments(
                    arguments, getattr(descriptor, "sensitive_fields", ())
                ),
                "node_id": node.id,
            },
        )
        started = time.perf_counter()

        async def execute() -> Any:
            result = await self.tool_gateway.execute(
                ToolInvocation(
                    execution_id=self.execution_id,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    arguments=arguments,
                    user_id=self.user.user_id,
                    approved=True,
                ),
                self.user,
            )
            if not result.ok:
                raise RuntimeError(result.error or f"tool returned status {result.status}")
            return result

        try:
            result = await self._with_retry(node, execute)
        except (GraphBubbleUp, asyncio.CancelledError):
            raise
        except Exception as exc:
            await self.sink.emit(
                "tool_call_completed",
                {
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "status": "failed",
                    "ok": False,
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                    "error": str(exc),
                    "node_id": node.id,
                },
            )
            raise
        duration_ms = int((time.perf_counter() - started) * 1000)
        await self.sink.emit(
            "tool_call_completed",
            {
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "status": result.status,
                "ok": result.ok,
                "duration_ms": duration_ms,
                "node_id": node.id,
            },
        )
        key = _output_key(config, "tool_output")
        return _Outcome(output=json_safe(result.output), variables={key: json_safe(result.output)})

    async def _knowledge(self, node: WorkflowNode, state: WorkflowState) -> _Outcome:
        if self.knowledge_gateway is None:
            raise RuntimeError("knowledge node requires a knowledge gateway")
        config = node.config
        variables = dict(state.get("variables") or {})
        query = render_text(str(config.get("query") or ""), variables)
        bases = render_value(config.get("knowledge_base_ids") or [], variables)
        if isinstance(bases, str):
            bases = [bases]
        top_k = int(config.get("top_k") or 5)
        metadata_filter = config.get("metadata_filter")
        if isinstance(metadata_filter, dict):
            metadata_filter = render_value(metadata_filter, variables)
        else:
            metadata_filter = None
        chunks = await self._with_retry(
            node,
            lambda: self.knowledge_gateway.search(  # type: ignore[union-attr]
                knowledge_base_ids=list(bases),
                query=query,
                user=self.user,
                top_k=top_k,
                metadata_filter=metadata_filter,
            ),
        )
        citations = [
            {
                "label": f"K{index}",
                "chunk_id": chunk.chunk_id,
                "document_id": chunk.document_id,
                "document_title": chunk.document_title,
                "knowledge_base_id": chunk.knowledge_base_id,
                "score": chunk.score,
                "content": chunk.content[:1200],
            }
            for index, chunk in enumerate(chunks, start=1)
        ]
        parts = ["## Retrieved Knowledge"]
        for index, chunk in enumerate(chunks, start=1):
            parts.append(
                f"[K{index}] {chunk.document_title} "
                f"(chunk={chunk.chunk_id}, score={chunk.score:.3f})\n"
                f"{chunk.content.strip()[:1200]}"
            )
        context = "\n\n".join(parts) if chunks else ""
        await self.sink.emit(
            "knowledge_retrieved",
            {"query": query, "count": len(citations), "citations": citations, "node_id": node.id},
        )
        key = _output_key(config, "knowledge")
        return _Outcome(
            output=context,
            variables={key: context},
            citations=citations,
        )

    async def _condition(self, node: WorkflowNode, state: WorkflowState) -> _Outcome:
        variables = dict(state.get("variables") or {})
        expression = str(node.config.get("expression") or "")
        result = evaluate_condition(expression, variables)
        return _Outcome(
            output={"expression": expression, "result": result},
            last_condition=result,
        )

    async def _approval(self, node: WorkflowNode, state: WorkflowState) -> _Outcome:
        config = node.config
        title = str(config.get("title") or node.name or node.id)
        description = str(config.get("description") or "")
        tool_call_id = f"node:{node.id}"
        handle = await self.sink.request_approval(
            tool_call_id=tool_call_id,
            tool_name=title,
            arguments=config,
            risk_level="high",
            kind="workflow_node",
            title=title,
            approval_key=f"node:{node.id}",
        )
        if handle.created:
            await self.sink.emit(
                "approval_required",
                {
                    "approval_id": handle.id,
                    "node_id": node.id,
                    "kind": "workflow_node",
                    "title": title,
                    "description": description,
                },
            )
        raw_decision = interrupt(
            {
                "approval_id": handle.id,
                "node_id": node.id,
                "kind": "workflow_node",
                "title": title,
                "description": description,
            }
        )
        decision = _normalize_decision(raw_decision)
        await self.sink.emit(
            "approval_resolved",
            {
                "approval_id": handle.id,
                "node_id": node.id,
                "kind": "workflow_node",
                "approved": decision["approved"],
                "comment": decision["comment"],
            },
        )
        output: dict[str, Any] = {
            "approved": decision["approved"],
            "comment": decision["comment"],
        }
        if not decision["approved"]:
            if str(config.get("on_reject") or "fail") != "continue":
                raise PermissionError(
                    f"approval node {node.id} was rejected"
                    + (f": {decision['comment']}" if decision["comment"] else "")
                )
            return _Outcome(output=output, variables={node.id: output}, status="rejected")
        return _Outcome(output=output, variables={node.id: output})

    async def _end(self, node: WorkflowNode, state: WorkflowState) -> _Outcome:
        variables = dict(state.get("variables") or {})
        config = node.config
        output_key = config.get("output_key")
        if isinstance(output_key, str) and _OUTPUT_KEY_RE.match(output_key):
            value = variables.get(output_key)
        elif self.definition.output.strip():
            value = render_text(self.definition.output, variables)
        else:
            value = state.get("last_step_output")
        text = _output_text(value)
        return _Outcome(output=text, final_output=text, variables={"output": text})

    # -- graph node wrapper ---------------------------------------------

    def _handler(self, node: WorkflowNode) -> Callable[[WorkflowState], Awaitable[_Outcome]]:
        handlers: dict[str, Callable[[WorkflowNode, WorkflowState], Awaitable[_Outcome]]] = {
            "start": self._start,
            "llm": self._llm,
            "agent": self._agent,
            "tool": self._tool,
            "knowledge": self._knowledge,
            "condition": self._condition,
            "approval": self._approval,
            "end": self._end,
        }
        return lambda state: handlers[node.type](node, state)

    def make_node(self, node: WorkflowNode) -> Callable[[WorkflowState], Awaitable[dict[str, Any]]]:
        handler = self._handler(node)

        async def run(state: WorkflowState) -> dict[str, Any]:
            started = time.perf_counter()
            await self.sink.emit(
                "workflow_node_started",
                {
                    "node_id": node.id,
                    "node_type": node.type,
                    "name": node.name or node.id,
                },
            )
            try:
                outcome = await handler(state)
            except (GraphBubbleUp, asyncio.CancelledError):
                raise
            except Exception as exc:
                duration_ms = int((time.perf_counter() - started) * 1000)
                error = f"{node.type} node '{node.id}' failed: {exc}"
                await self.sink.emit(
                    "workflow_node_failed",
                    {
                        "node_id": node.id,
                        "node_type": node.type,
                        "name": node.name or node.id,
                        "error": str(exc),
                        "duration_ms": duration_ms,
                    },
                )
                step = {
                    "node_id": node.id,
                    "node_type": node.type,
                    "status": "failed",
                    "error": str(exc),
                    "duration_ms": duration_ms,
                }
                return {
                    "error": error,
                    "steps": [*state.get("steps", []), step],
                    "last_step_output": None,
                }

            duration_ms = int((time.perf_counter() - started) * 1000)
            variables = json_safe(dict(state.get("variables") or {}))
            if outcome.variables:
                variables.update(json_safe(outcome.variables))
            node_outputs = dict(state.get("node_outputs") or {})
            output = json_safe(outcome.output)
            node_outputs[node.id] = output
            citations = list(state.get("citations") or [])
            if outcome.citations:
                citations.extend(outcome.citations)
            preview = _preview(output)
            await self.sink.emit(
                "workflow_node_completed",
                {
                    "node_id": node.id,
                    "node_type": node.type,
                    "name": node.name or node.id,
                    "status": outcome.status,
                    "output_preview": preview,
                    "duration_ms": duration_ms,
                },
            )
            step = {
                "node_id": node.id,
                "node_type": node.type,
                "status": outcome.status,
                "output_preview": preview,
                "duration_ms": duration_ms,
            }
            update: dict[str, Any] = {
                "variables": variables,
                "steps": [*state.get("steps", []), step],
                "node_outputs": node_outputs,
                "citations": citations,
                "last_step_output": output,
                "error": None,
            }
            if outcome.usage is not None:
                update["usage"] = outcome.usage
            if outcome.last_condition is not None:
                update["last_condition"] = outcome.last_condition
            if outcome.final_output is not None:
                update["final_output"] = outcome.final_output
            return update

        return run


def build_workflow_graph(
    *,
    snapshot: WorkflowSnapshot,
    model_gateway: ModelGateway,
    tool_gateway: ToolGateway,
    sink: ExecutionSink,
    user: UserContext,
    execution_id: str,
    knowledge_gateway: KnowledgeGateway | None = None,
    skill_gateway: SkillGateway | None = None,
    agent_loader: AgentLoader | None = None,
    checkpointer: Any = None,
) -> CompiledStateGraph:
    """Compile the frozen definition into a LangGraph state graph."""

    definition = snapshot.definition
    executor = _WorkflowExecutor(
        snapshot=snapshot,
        model_gateway=model_gateway,
        tool_gateway=tool_gateway,
        knowledge_gateway=knowledge_gateway,
        skill_gateway=skill_gateway,
        agent_loader=agent_loader,
        sink=sink,
        user=user,
        execution_id=execution_id,
    )
    graph: StateGraph = StateGraph(WorkflowState)
    nodes = {node.id: node for node in definition.nodes}
    for node in definition.nodes:
        graph.add_node(node.id, executor.make_node(node))

    start_id = definition.start_node or next(
        (node.id for node in definition.nodes if node.type == "start"), None
    )
    if start_id is None:  # pragma: no cover - validation rejects this
        raise ValueError("workflow has no start node")
    graph.add_edge(START, start_id)

    for node in definition.nodes:
        if node.type == "end":
            graph.add_edge(node.id, END)
            continue
        if node.type == "condition":
            targets = {
                "true": node.on_true or "",
                "false": node.on_false or "",
                END: END,
            }

            def route_condition(state: WorkflowState, *, targets: dict[str, str] = targets) -> str:
                if state.get("error"):
                    return END
                return "true" if state.get("last_condition") else "false"

            graph.add_conditional_edges(node.id, route_condition, targets)
            continue
        target = node.next
        if not target:  # pragma: no cover - validation rejects this
            continue
        if target not in nodes:  # pragma: no cover - validation rejects this
            continue
        targets = {target: target, END: END}

        def route_next(state: WorkflowState, *, target: str = target) -> str:
            if state.get("error"):
                return END
            return target

        graph.add_conditional_edges(node.id, route_next, targets)
    return graph.compile(checkpointer=checkpointer)


__all__ = [
    "WorkflowState",
    "build_workflow_graph",
    "evaluate_condition",
    "json_safe",
    "render_text",
    "render_value",
]
