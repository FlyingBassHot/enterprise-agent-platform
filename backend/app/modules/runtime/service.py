"""Runtime adapter over LangGraph.

Depends only on ``app.core.contracts`` gateways and the execution sink
protocol; the execution module owns persistence.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from langgraph.types import Command

from app.core.config import get_settings
from app.core.contracts import (
    AgentSnapshot,
    ChatMessage,
    ModelGateway,
    SkillContext,
    SkillGateway,
    ToolGateway,
    UserContext,
)
from app.core.logging import get_logger
from app.modules.runtime.checkpoint import get_checkpointer
from app.modules.runtime.context import (
    AgentState,
    ExecutionSink,
    build_initial_messages,
)
from app.modules.runtime.graph import build_agent_graph

logger = get_logger(__name__)


@dataclass(slots=True)
class RuntimeResult:
    status: str
    output_text: str | None = None
    output_payload: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    approval_ids: list[str] = field(default_factory=list)


def _interrupt_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


class RuntimeService:
    def __init__(
        self,
        *,
        model_gateway: ModelGateway,
        tool_gateway: ToolGateway,
        sink: ExecutionSink,
        skill_gateway: SkillGateway | None = None,
        checkpointer: Any = None,
    ) -> None:
        self.model_gateway = model_gateway
        self.tool_gateway = tool_gateway
        self.sink = sink
        self.skill_gateway = skill_gateway
        self._checkpointer = checkpointer

    async def _resolve_checkpointer(self) -> Any:
        if self._checkpointer is not None:
            return self._checkpointer
        return await get_checkpointer()

    async def _resolve_skills(
        self, snapshot: AgentSnapshot, user: UserContext
    ) -> list[SkillContext]:
        if not snapshot.skill_names or self.skill_gateway is None:
            return []
        try:
            return await self.skill_gateway.resolve_for_agent(snapshot.skill_names, user)
        except Exception as exc:  # noqa: BLE001 - skills are additive context
            logger.warning("skill resolution failed: %s", exc, extra={"execution_id": None})
            return []

    def _graph(self, snapshot: AgentSnapshot, user: UserContext, checkpointer: Any, skills: list[SkillContext]):
        return build_agent_graph(
            model_gateway=self.model_gateway,
            tool_gateway=self.tool_gateway,
            sink=self.sink,
            user=user,
            skills=skills,
            checkpointer=checkpointer,
        )

    @staticmethod
    def _config(execution_id: str, snapshot: AgentSnapshot | None) -> dict[str, Any]:
        settings = get_settings()
        iterations = int(
            (snapshot.max_iterations if snapshot else settings.execution_max_iterations)
            or settings.execution_max_iterations
        )
        return {
            "configurable": {"thread_id": execution_id},
            "recursion_limit": max(25, iterations * 4 + 10),
        }

    @staticmethod
    def _timeout(snapshot: AgentSnapshot | None) -> int:
        settings = get_settings()
        return max(
            1,
            int(
                (snapshot.timeout_seconds if snapshot else settings.execution_timeout_seconds)
                or settings.execution_timeout_seconds
            ),
        )

    async def run(
        self,
        *,
        execution_id: str,
        snapshot: AgentSnapshot,
        user: UserContext,
        history: Sequence[ChatMessage] = (),
        user_text: str = "",
        variables: dict[str, Any] | None = None,
    ) -> RuntimeResult:
        skills = await self._resolve_skills(snapshot, user)
        messages = build_initial_messages(
            snapshot,
            history=history,
            user_text=user_text,
            variables=variables or {},
            skills=skills,
        )
        state: AgentState = {
            "execution_id": execution_id,
            "agent_snapshot": snapshot.model_dump(),
            "messages": messages,
            "pending_tool_calls": [],
            "steps": 0,
            "final_output": None,
            "error": None,
            "usage": {},
        }
        checkpointer = await self._resolve_checkpointer()
        graph = self._graph(snapshot, user, checkpointer, skills)
        config = self._config(execution_id, snapshot)
        try:
            async with asyncio.timeout(self._timeout(snapshot)):
                result = await graph.ainvoke(state, config)
        except TimeoutError:
            return RuntimeResult(
                status="failed",
                error=f"execution timed out after {self._timeout(snapshot)}s",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - graph failures become failed executions
            logger.exception("runtime execution failed", extra={"execution_id": execution_id})
            return RuntimeResult(status="failed", error=f"runtime error: {exc}")
        return await self._result_from_state(result, execution_id)

    async def resume(
        self,
        *,
        execution_id: str,
        user: UserContext,
        snapshot: AgentSnapshot | None = None,
        decision: dict[str, Any] | None = None,
        interrupt_id: str | None = None,
    ) -> RuntimeResult:
        checkpointer = await self._resolve_checkpointer()
        skills = await self._resolve_skills(snapshot, user) if snapshot else []
        graph = self._graph(snapshot, user, checkpointer, skills)  # type: ignore[arg-type]
        config = self._config(execution_id, snapshot)
        if decision is None and interrupt_id is None:
            graph_input: Any = None
        elif interrupt_id:
            graph_input = Command(resume={interrupt_id: decision})
        else:
            graph_input = Command(resume=decision)
        try:
            async with asyncio.timeout(self._timeout(snapshot)):
                result = await graph.ainvoke(graph_input, config)
        except TimeoutError:
            return RuntimeResult(
                status="failed",
                error=f"execution timed out after {self._timeout(snapshot)}s",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("runtime resume failed", extra={"execution_id": execution_id})
            return RuntimeResult(status="failed", error=f"runtime error: {exc}")
        return await self._result_from_state(result, execution_id)

    async def _result_from_state(self, result: Any, execution_id: str) -> RuntimeResult:
        state: dict[str, Any] = result if isinstance(result, dict) else {}
        interrupts = state.get("__interrupt__") or []
        usage = dict(state.get("usage") or {})
        messages = state.get("messages") or []
        last_assistant = next(
            (item for item in reversed(messages) if item.get("role") == "assistant"), {}
        )
        output_payload = {
            "steps": state.get("steps", 0),
            "tool_calls": last_assistant.get("tool_calls") or [],
        }
        if interrupts:
            approval_ids: list[str] = []
            interrupt_map: dict[str, str] = {}
            for item in interrupts:
                value = getattr(item, "value", None)
                interrupt_id = getattr(item, "id", None)
                if isinstance(item, dict):
                    value = item.get("value", value)
                    interrupt_id = item.get("id", interrupt_id)
                payload = _interrupt_payload(value)
                approval_id = payload.get("approval_id")
                if approval_id and interrupt_id:
                    interrupt_map[str(approval_id)] = str(interrupt_id)
                if approval_id:
                    approval_ids.append(str(approval_id))
            if interrupt_map:
                await self.sink.attach_interrupt_ids(interrupt_map)
            return RuntimeResult(
                status="waiting_approval",
                output_text=last_assistant.get("content") or None,
                output_payload=output_payload,
                usage=usage,
                approval_ids=approval_ids,
            )
        error = state.get("error")
        if error:
            return RuntimeResult(
                status="failed",
                output_text=last_assistant.get("content") or None,
                output_payload=output_payload,
                usage=usage,
                error=str(error),
            )
        final_output = state.get("final_output")
        if final_output is None:
            return RuntimeResult(
                status="failed",
                output_text=None,
                output_payload=output_payload,
                usage=usage,
                error="agent finished without producing an answer",
            )
        return RuntimeResult(
            status="completed",
            output_text=str(final_output),
            output_payload=output_payload,
            usage=usage,
        )


__all__ = ["RuntimeResult", "RuntimeService"]
