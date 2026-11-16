"""LangGraph wiring for one agent execution.

The graph is intentionally built per run so it can close over the user context
and the execution sink; the checkpointer carries the durable state between
runs (including approval interrupts).
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.core.config import get_settings
from app.core.contracts import (
    AgentSnapshot,
    ChatMessage,
    ModelGateway,
    SkillContext,
    ToolCall,
    ToolDescriptor,
    ToolGateway,
    ToolInvocation,
    UserContext,
)
from app.core.plan import PLAN_TOOL_NAME, normalize_plan, render_plan_section
from app.modules.runtime.context import (
    AgentState,
    ExecutionSink,
    LLMCallRecord,
    accumulate_usage,
    mask_arguments,
    to_chat_messages,
    truncate_tool_output,
)

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph


def _normalize_decision(raw: Any) -> dict[str, Any]:
    if isinstance(raw, bool):
        return {"approved": raw, "comment": ""}
    if isinstance(raw, dict):
        approved = raw.get("approved")
        if isinstance(approved, str):
            approved = approved.strip().lower() in {"1", "true", "yes", "approved"}
        return {"approved": bool(approved), "comment": str(raw.get("comment") or "")}
    return {"approved": False, "comment": ""}


def build_agent_graph(
    *,
    model_gateway: ModelGateway,
    tool_gateway: ToolGateway,
    sink: ExecutionSink,
    user: UserContext,
    skills: list[SkillContext] | None = None,
    checkpointer: Any = None,
) -> CompiledStateGraph:
    settings = get_settings()

    async def agent_node(state: AgentState) -> dict[str, Any]:
        snapshot = AgentSnapshot.model_validate(state["agent_snapshot"])
        execution_id = state.get("execution_id", "")
        steps = int(state.get("steps", 0))
        max_iterations = int(
            state.get("max_iterations")
            or snapshot.max_iterations
            or settings.execution_max_iterations
        )
        messages = list(state.get("messages", []))
        if steps >= max_iterations:
            # The loop is out of budget. A partial answer is still worth
            # returning: the execution becomes resumable instead of failing.
            partial = next(
                (
                    str(message.get("content"))
                    for message in reversed(messages)
                    if message.get("role") == "assistant" and message.get("content")
                ),
                None,
            )
            if partial:
                return {
                    "error": None,
                    "steps": steps,
                    "pending_tool_calls": [],
                    "final_output": partial,
                    "stop_reason": "max_iterations",
                }
            return {
                "error": (
                    f"maximum iterations ({max_iterations}) exceeded "
                    "before producing an answer"
                ),
                "steps": steps,
                "pending_tool_calls": [],
                "final_output": None,
                "stop_reason": "max_iterations",
            }

        chat_messages = to_chat_messages(messages)
        plan = list(state.get("plan") or [])
        if plan:
            # Re-inject the plan on every turn: it is state, not history, so it
            # cannot drift or be dropped by context trimming.
            index = 1 if chat_messages and chat_messages[0].role == "system" else 0
            chat_messages.insert(
                index, ChatMessage(role="system", content=render_plan_section(plan))
            )

        stored = state.get("tool_descriptors")
        if stored is None:
            # Legacy checkpoints predate the cached descriptors.
            descriptors: list[ToolDescriptor] = []
            if snapshot.tool_names:
                try:
                    descriptors = await tool_gateway.describe(snapshot.tool_names, user)
                except Exception as exc:  # noqa: BLE001 - registry failure is fatal here
                    return {
                        "error": f"failed to resolve tools: {exc}",
                        "steps": steps,
                        "pending_tool_calls": [],
                        "final_output": None,
                    }
        else:
            descriptors = [ToolDescriptor.model_validate(item) for item in stored]
        specs = [descriptor.to_spec() for descriptor in descriptors if descriptor.enabled]

        provider = ""
        model_name = ""
        model_config_id = snapshot.model.config_id
        try:
            resolved = await model_gateway.resolve(model_config_id)
            provider = resolved.provider_name or resolved.provider_type
            model_name = resolved.model
            if model_config_id is None:
                model_config_id = resolved.config_id
        except Exception:  # noqa: BLE001 - surfaced by the model call below
            provider = "unknown"

        started = time.perf_counter()
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        usage = accumulate_usage(state.get("usage", {}), 0, 0, 0)
        error: str | None = None
        timeout_seconds = max(1, int(snapshot.timeout_seconds or settings.execution_timeout_seconds))
        try:
            async with asyncio.timeout(timeout_seconds):
                stream = model_gateway.stream_chat(
                    model_config_id=model_config_id,
                    messages=chat_messages,
                    tools=specs,
                    temperature=snapshot.model.temperature,
                    max_tokens=snapshot.model.max_tokens,
                    execution_id=execution_id,
                )
                async for event in stream:
                    if event.kind == "delta" and event.content:
                        text_parts.append(event.content)
                        await sink.emit(
                            "message_delta",
                            {"content": event.content, "delta": event.content},
                        )
                    elif event.kind == "tool_call" and event.tool_call is not None:
                        tool_calls.append(event.tool_call)
                    elif event.kind == "done":
                        usage = accumulate_usage(
                            state.get("usage", {}),
                            event.usage.prompt_tokens,
                            event.usage.completion_tokens,
                            event.usage.total_tokens,
                        )
        except TimeoutError:
            error = f"model call timed out after {timeout_seconds}s"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - model errors surface as failed executions
            error = f"model call failed: {exc}"

        latency_ms = int((time.perf_counter() - started) * 1000)
        await sink.record_llm_call(
            LLMCallRecord(
                model_config_id=model_config_id,
                provider=provider,
                model=model_name,
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                total_tokens=usage.get("total_tokens", 0),
                latency_ms=latency_ms,
                status="failed" if error else "success",
                error=error,
            )
        )

        text = "".join(text_parts)
        assistant_message: dict[str, Any] = {"role": "assistant", "content": text}
        if tool_calls:
            assistant_message["tool_calls"] = [call.model_dump() for call in tool_calls]
        messages.append(assistant_message)

        await sink.emit(
            "message_completed",
            {
                "role": "assistant",
                "content": text,
                "tool_calls": [call.model_dump() for call in tool_calls],
                "finish_reason": "tool_calls" if tool_calls else "stop",
            },
        )

        next_steps = steps + 1
        if error:
            return {
                "messages": messages,
                "steps": next_steps,
                "error": error,
                "pending_tool_calls": [],
                "final_output": None,
                "usage": usage,
            }
        if tool_calls:
            return {
                "messages": messages,
                "steps": next_steps,
                "error": None,
                "pending_tool_calls": [call.model_dump() for call in tool_calls],
                "final_output": None,
                "stop_reason": None,
                "usage": usage,
            }
        return {
            "messages": messages,
            "steps": next_steps,
            "error": None,
            "pending_tool_calls": [],
            "final_output": text,
            "stop_reason": None,
            "usage": usage,
        }

    async def tools_node(state: AgentState) -> dict[str, Any]:
        snapshot = AgentSnapshot.model_validate(state["agent_snapshot"])
        execution_id = state.get("execution_id", "")
        pending: list[dict[str, Any]] = list(state.get("pending_tool_calls") or [])
        messages = list(state.get("messages", []))
        approvals_configured = set(snapshot.approval_risk_levels)
        bound_tools = set(snapshot.tool_names)
        platform_approval_levels = {"high", "critical"}

        pinned_versions = dict(snapshot.tool_versions or {})
        descriptor_map: dict[str, Any] = {}
        plan = list(state.get("plan") or [])
        plan_changed = False
        if pending:
            pending_names = {str(call.get("name")) for call in pending}
            stored = state.get("tool_descriptors") or []
            for raw in stored:
                descriptor = ToolDescriptor.model_validate(raw)
                if descriptor.name not in pending_names or descriptor.name not in bound_tools:
                    continue
                if pinned_versions and pinned_versions.get(descriptor.name) != descriptor.version:
                    continue
                descriptor_map[descriptor.name] = descriptor

        # Phase 1: resolve every approval before executing anything, so a
        # resumed node never replays already executed tool side effects.
        decisions: dict[str, dict[str, Any]] = {}
        for call_index, call in enumerate(pending):
            call_id = str(call.get("id") or "")
            approval_key = f"step:{int(state.get('steps', 0))}:call:{call_index}"
            name = str(call.get("name") or "")
            descriptor = descriptor_map.get(name)
            # Agent configuration may add approval requirements, but platform
            # policy (risk level / explicit flag) can never be weakened by it.
            needs_approval = bool(
                descriptor is not None
                and descriptor.enabled
                and (
                    descriptor.requires_approval
                    or descriptor.risk_level in platform_approval_levels
                    or descriptor.risk_level in approvals_configured
                )
            )
            if not needs_approval:
                decisions[call_id] = {"approved": True, "comment": ""}
                continue
            raw_arguments = dict(call.get("arguments") or {})
            safe_arguments = mask_arguments(
                raw_arguments, getattr(descriptor, "sensitive_fields", ())
            )
            handle = await sink.request_approval(
                tool_call_id=call_id,
                tool_name=name,
                arguments=safe_arguments,
                risk_level=descriptor.risk_level,
                approval_key=approval_key,
            )
            if handle.created:
                await sink.emit(
                    "approval_required",
                    {
                        "approval_id": handle.id,
                        "tool_call_id": call_id,
                        "tool_name": name,
                        "arguments": safe_arguments,
                        "risk_level": descriptor.risk_level,
                        "approval_key": approval_key,
                    },
                )
            raw_decision = interrupt(
                {
                    "approval_id": handle.id,
                    "tool": name,
                    "tool_call_id": call_id,
                    "arguments": safe_arguments,
                    "risk_level": descriptor.risk_level,
                    "approval_key": approval_key,
                }
            )
            decision = _normalize_decision(raw_decision)
            decisions[call_id] = decision
            await sink.emit(
                "approval_resolved",
                {
                    "approval_id": handle.id,
                    "tool_call_id": call_id,
                    "tool_name": name,
                    "approved": decision["approved"],
                    "comment": decision["comment"],
                },
            )

        # Phase 2: execute the calls.
        tool_messages: list[dict[str, Any]] = []
        for call in pending:
            call_id = str(call.get("id") or "")
            name = str(call.get("name") or "")
            arguments = dict(call.get("arguments") or {})
            descriptor = descriptor_map.get(name)
            decision = decisions.get(call_id) or {"approved": False, "comment": ""}

            if not decision.get("approved"):
                content = json.dumps(
                    {
                        "status": "rejected",
                        "ok": False,
                        "tool": name,
                        "comment": decision.get("comment", ""),
                        "message": "The user rejected this tool call; do not retry it.",
                    },
                    ensure_ascii=False,
                )
                tool_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "name": name,
                        "content": content,
                    }
                )
                await sink.emit(
                    "tool_call_completed",
                    {
                        "tool_call_id": call_id,
                        "tool_name": name,
                        "status": "rejected",
                        "ok": False,
                        "error": decision.get("comment") or "rejected by user",
                    },
                )
                continue

            if descriptor is None or not descriptor.enabled or name not in bound_tools:
                reason = (
                    "tool is not bound to this agent"
                    if name not in bound_tools
                    else "unknown or disabled tool"
                )
                content = json.dumps(
                    {"status": "failed", "ok": False, "error": f"{reason}: {name}"},
                    ensure_ascii=False,
                )
                tool_messages.append(
                    {"role": "tool", "tool_call_id": call_id, "name": name, "content": content}
                )
                await sink.emit(
                    "tool_call_completed",
                    {
                        "tool_call_id": call_id,
                        "tool_name": name,
                        "status": "failed",
                        "ok": False,
                        "error": reason,
                    },
                )
                continue

            await sink.emit(
                "tool_call_started",
                {
                    "tool_call_id": call_id,
                    "tool_name": name,
                    "arguments": mask_arguments(
                        arguments, getattr(descriptor, "sensitive_fields", ())
                    ),
                },
            )
            started = time.perf_counter()
            status = "success"
            ok = False
            output: Any = None
            result_error: str | None = None
            try:
                result = await tool_gateway.execute(
                    ToolInvocation(
                        execution_id=execution_id,
                        tool_call_id=call_id,
                        tool_name=name,
                        arguments=arguments,
                        user_id=user.user_id,
                        agent_id=snapshot.agent_id,
                        approved=True,
                    ),
                    user,
                )
                status = result.status
                ok = result.ok
                output = result.output
                result_error = result.error
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - tool failures become tool messages
                status = "failed"
                result_error = str(exc)
            duration_ms = int((time.perf_counter() - started) * 1000)

            content_obj = {
                "status": status,
                "ok": ok,
                "output": output,
                "error": result_error,
            }
            tool_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": name,
                    "content": truncate_tool_output(
                        json.dumps(content_obj, ensure_ascii=False, default=str)
                    ),
                }
            )
            await sink.emit(
                "tool_call_completed",
                {
                    "tool_call_id": call_id,
                    "tool_name": name,
                    "status": status,
                    "ok": ok,
                    "duration_ms": duration_ms,
                    "error": result_error,
                },
            )
            if ok and name == PLAN_TOOL_NAME and isinstance(output, dict):
                normalized = normalize_plan(output.get("plan"))
                if normalized is not None and normalized != plan:
                    plan = normalized
                    plan_changed = True
                    await sink.emit("plan_updated", {"plan": plan})

        update: dict[str, Any] = {
            "messages": messages + tool_messages,
            "pending_tool_calls": [],
            "error": None,
        }
        if plan_changed:
            update["plan"] = plan
        return update

    def route_after_agent(state: AgentState) -> str:
        if state.get("error"):
            return END
        if state.get("pending_tool_calls"):
            return "tools"
        return END

    def route_after_tools(state: AgentState) -> str:
        if state.get("error"):
            return END
        return "agent"

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tools_node)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route_after_agent, {"tools": "tools", END: END})
    graph.add_conditional_edges("tools", route_after_tools, {"agent": "agent", END: END})
    return graph.compile(checkpointer=checkpointer)


__all__ = ["build_agent_graph"]
