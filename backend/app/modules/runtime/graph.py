"""LangGraph wiring for one agent execution.

The graph is intentionally built per run so it can close over the user context
and the execution sink; the checkpointer carries the durable state between
runs (including approval interrupts).
"""

from __future__ import annotations

import asyncio
import hashlib
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
from app.core.policy import PolicyEffect, decide_tool_call
from app.modules.runtime.context import (
    AgentState,
    ExecutionSink,
    LLMCallRecord,
    accumulate_usage,
    compact_messages,
    completion_feedback,
    mask_arguments,
    to_chat_messages,
    tool_error_content,
    truncate_tool_output,
)

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph


def _tool_signature(name: str, arguments: Any) -> str:
    payload = json.dumps(arguments or {}, sort_keys=True, ensure_ascii=False, default=str)
    return f"{name}:{hashlib.sha1(payload.encode('utf-8')).hexdigest()[:16]}"


def _result_digest(payload: Any) -> str:
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


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
    task_gateway: Any = None,
    verifier_gateway: Any = None,
) -> CompiledStateGraph:
    settings = get_settings()

    def _last_assistant_text(messages: list[dict[str, Any]]) -> str | None:
        """Last assistant text from *this* turn, never from conversation history."""

        for message in reversed(messages):
            if message.get("role") == "user":
                break
            if message.get("role") == "assistant" and message.get("content"):
                return str(message["content"])
        return None

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
        partial = _last_assistant_text(messages)
        token_budget = state.get("token_budget")
        used_tokens = int((state.get("usage") or {}).get("total_tokens", 0))
        if token_budget and used_tokens >= int(token_budget):
            # Resource guard: never start another model call past the cap.
            await sink.emit(
                "budget_exhausted",
                {"kind": "tokens", "used": used_tokens, "limit": int(token_budget)},
            )
            return {
                "error": None,
                "steps": steps,
                "pending_tool_calls": [],
                "final_output": partial,
                "stop_reason": "token_budget",
            }
        if steps >= max_iterations:
            # The loop is out of budget. A partial answer is still worth
            # returning: the execution becomes resumable instead of failing.
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

        compacted, saved = compact_messages(
            messages,
            budget_chars=int(settings.context_char_budget),
            keep_recent=int(settings.context_keep_recent_messages),
        )
        if saved > 0:
            await sink.emit(
                "context_compacted",
                {
                    "chars_saved": saved,
                    "budget_chars": int(settings.context_char_budget),
                    "messages": len(messages),
                },
            )
        chat_messages = to_chat_messages(compacted)
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
        if token_budget and int(usage.get("total_tokens", 0)) >= int(token_budget):
            await sink.emit(
                "budget_exhausted",
                {
                    "kind": "tokens",
                    "used": int(usage.get("total_tokens", 0)),
                    "limit": int(token_budget),
                },
            )
            return {
                "messages": messages,
                "steps": next_steps,
                "error": None,
                "pending_tool_calls": [],
                "final_output": text or _last_assistant_text(messages),
                "stop_reason": "token_budget",
                "usage": usage,
            }
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
        bound_tools = set(snapshot.tool_names)

        pinned_versions = dict(snapshot.tool_versions or {})
        descriptor_map: dict[str, Any] = {}
        plan = list(state.get("plan") or [])
        plan_changed = False
        last_signature = state.get("last_tool_signature")
        last_digest = state.get("last_tool_digest")
        repeat_streak = int(state.get("repeat_streak") or 0)
        no_progress = False
        if pending:
            pending_names = {str(call.get("name")) for call in pending}
            stored = state.get("tool_descriptors") or []
            for raw in stored:
                descriptor = ToolDescriptor.model_validate(raw)
                if descriptor.name not in pending_names:
                    continue
                if pinned_versions and pinned_versions.get(descriptor.name) != descriptor.version:
                    continue
                descriptor_map[descriptor.name] = descriptor

        # Phase 1: resolve every approval before executing anything, so a
        # resumed node never replays already executed tool side effects.
        decisions: dict[int, dict[str, Any]] = {}
        seen_call_ids: set[str] = set()
        for call_index, call in enumerate(pending):
            call_id = str(call.get("id") or "")
            approval_key = f"step:{int(state.get('steps', 0))}:call:{call_index}"
            name = str(call.get("name") or "")
            if not call_id or call_id in seen_call_ids:
                # Provider-supplied ids are untrusted: an empty or duplicate id
                # could let one verdict overwrite another. Fail it shut.
                decisions[call_index] = {
                    "approved": False,
                    "invalid": True,
                    "comment": (
                        f"duplicate tool_call id: {call_id}"
                        if call_id
                        else "empty tool_call id"
                    ),
                }
                continue
            seen_call_ids.add(call_id)
            descriptor = descriptor_map.get(name)
            # One decision function for availability, binding, access and
            # approval. Agent config may add approvals; it can never weaken
            # the platform floor.
            policy = decide_tool_call(
                descriptor,
                bound=name in bound_tools,
                access_allowed=True,
                agent_approval_levels=snapshot.approval_risk_levels,
            )
            if policy.effect is PolicyEffect.DENY:
                decisions[call_index] = {
                    "approved": False,
                    "comment": policy.reason,
                    "policy": policy,
                }
                continue
            signature = _tool_signature(name, call.get("arguments") or {})
            if signature == last_signature and repeat_streak >= 2:
                decisions[call_index] = {
                    "approved": False,
                    "comment": "identical tool call repeated without new information",
                    "repeated": True,
                }
                continue
            if policy.effect is not PolicyEffect.REQUIRE_APPROVAL:
                decisions[call_index] = {"approved": True, "comment": "", "policy": policy}
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
                        "policy": policy.model_dump(),
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
                    "policy": policy.model_dump(),
                }
            )
            decision = _normalize_decision(raw_decision)
            decisions[call_index] = decision
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
        for call_index, call in enumerate(pending):
            call_id = str(call.get("id") or "")
            name = str(call.get("name") or "")
            arguments = dict(call.get("arguments") or {})
            descriptor = descriptor_map.get(name)
            decision = decisions.get(call_index) or {"approved": False, "comment": ""}
            if decision.get("invalid"):
                content = json.dumps(
                    tool_error_content(
                        "failed",
                        error=str(decision.get("comment") or "invalid tool call"),
                        tool=name,
                        code="invalid_call",
                        hint="同一轮工具调用 id 重复；请重新规划并只提交一次。",
                        retryable=False,
                    ),
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
                        "status": "denied",
                        "ok": False,
                        "error": "invalid_call",
                        "error_code": "invalid_call",
                    },
                )
                continue
            policy = decision.get("policy")

            if policy is not None and policy.effect is PolicyEffect.DENY:
                content = json.dumps(
                    tool_error_content(
                        "failed",
                        error=policy.reason,
                        tool=name,
                        code=policy.code,
                        hint="策略拒绝该调用；不要重试，改用允许的方式或说明限制。",
                        retryable=False,
                    ),
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
                        "status": "denied",
                        "ok": False,
                        "error": policy.reason,
                        "error_code": policy.code,
                    },
                )
                continue

            signature = _tool_signature(name, arguments)
            if decision.get("repeated") or (
                signature == last_signature and repeat_streak >= 2
            ):
                no_progress = True
                content = json.dumps(
                    tool_error_content(
                        "failed",
                        error="相同工具调用已连续执行且结果一致，停止重复。",
                        tool=name,
                        code="repeated_call",
                        hint="不要再次提交相同调用；更换参数、换工具，或说明无法继续。",
                        retryable=False,
                    ),
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
                    "no_progress_detected",
                    {
                        "tool_call_id": call_id,
                        "tool_name": name,
                        "signature": _tool_signature(name, arguments),
                        "streak": repeat_streak,
                    },
                )
                await sink.emit(
                    "tool_call_completed",
                    {
                        "tool_call_id": call_id,
                        "tool_name": name,
                        "status": "denied",
                        "ok": False,
                        "error": "repeated_call",
                        "error_code": "repeated_call",
                    },
                )
                continue

            if not decision.get("approved"):
                content = json.dumps(
                    tool_error_content(
                        "approval_rejected",
                        error=decision.get("comment") or "rejected by user",
                        tool=name,
                    ),
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
                        "error_code": "approval_rejected",
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
                    tool_error_content(
                        "tool_unavailable", error=f"{reason}: {name}", tool=name
                    ),
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

            if ok:
                content_obj = {
                    "status": status,
                    "ok": True,
                    "output": output,
                    "error": None,
                }
            else:
                content_obj = tool_error_content(
                    status, error=result_error, output=output, tool=name
                )
            digest = _result_digest(content_obj)
            if signature == last_signature and digest == last_digest:
                repeat_streak += 1
            else:
                repeat_streak = 1
            last_signature, last_digest = signature, digest
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
                    if task_gateway is not None:
                        # Persist on the task, not the execution: a later
                        # execution continues from the same plan.
                        await task_gateway.apply_plan(execution_id, plan)
                    await sink.emit("plan_updated", {"plan": plan})

        update: dict[str, Any] = {
            "messages": messages + tool_messages,
            "pending_tool_calls": [],
            "error": None,
            "last_tool_signature": last_signature,
            "last_tool_digest": last_digest,
            "repeat_streak": repeat_streak,
        }
        if plan_changed:
            update["plan"] = plan
        if no_progress:
            update["stop_reason"] = "no_progress"
        return update

    async def verify_node(state: AgentState) -> dict[str, Any]:
        snapshot = AgentSnapshot.model_validate(state["agent_snapshot"])
        execution_id = state.get("execution_id", "")
        attempts = int(state.get("verification_attempts", 0)) + 1
        if not snapshot.verification_enabled or verifier_gateway is None:
            return {
                "verification_status": "skipped",
                "verification_issues": [],
                "verification_attempts": attempts,
            }

        plan = list(state.get("plan") or [])
        citations = list(state.get("knowledge_citations") or [])
        text = str(state.get("final_output") or "")
        await sink.emit(
            "verification_started",
            {
                "attempt": attempts,
                "plan_steps": len(plan),
                "citations": len(citations),
                "answer_chars": len(text),
            },
        )
        outcome = await verifier_gateway.verify(
            execution_id=execution_id,
            snapshot=snapshot,
            plan=plan,
            output_text=text,
            citations=citations,
        )
        issues = [issue.model_dump() for issue in outcome.issues]
        await sink.emit(
            "verification_completed",
            {
                "status": outcome.status,
                "attempt": attempts,
                "next_action": outcome.next_action,
                "issues": issues,
            },
        )
        if outcome.status == "passed":
            return {
                "verification_status": "passed",
                "verification_issues": [],
                "verification_attempts": attempts,
            }

        max_retries = max(0, int(snapshot.verification_max_retries))
        if attempts <= max_retries:
            messages = list(state.get("messages", []))
            messages.append(
                {"role": "system", "content": completion_feedback(issues)}
            )
            return {
                "verification_status": "failed",
                "verification_issues": issues,
                "verification_attempts": attempts,
                "messages": messages,
                "final_output": None,
                "stop_reason": None,
            }
        # Out of retries: keep the answer, let the execution end as incomplete.
        return {
            "verification_status": "failed",
            "verification_issues": issues,
            "verification_attempts": attempts,
            "stop_reason": "verification_failed",
        }

    def route_after_agent(state: AgentState) -> str:
        if state.get("error"):
            return END
        if state.get("pending_tool_calls"):
            return "tools"
        if state.get("stop_reason") in {"max_iterations", "token_budget"}:
            # A truncated answer is not a completion claim: keep the budget
            # reason and do not spend a verification retry on it.
            return END
        return "verify"

    def route_after_verify(state: AgentState) -> str:
        if state.get("stop_reason") == "verification_failed":
            return END
        if state.get("verification_status") == "failed":
            return "agent"
        return END

    def route_after_tools(state: AgentState) -> str:
        if state.get("error"):
            return END
        if state.get("stop_reason") == "no_progress":
            return END
        return "agent"

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tools_node)
    graph.add_node("verify", verify_node)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges(
        "agent", route_after_agent, {"tools": "tools", "verify": "verify", END: END}
    )
    graph.add_conditional_edges("tools", route_after_tools, {"agent": "agent", END: END})
    graph.add_conditional_edges(
        "verify", route_after_verify, {"agent": "agent", END: END}
    )
    return graph.compile(checkpointer=checkpointer)


__all__ = ["build_agent_graph"]
