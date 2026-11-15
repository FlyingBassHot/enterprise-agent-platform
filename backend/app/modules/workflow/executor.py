"""Workflow runtime: executes frozen workflow snapshots through LangGraph.

``ExecutionService`` owns persistence and passes its sink in; this module only
knows about the gateway contracts and the graph engine.
"""

from __future__ import annotations

import asyncio
from typing import Any

from langgraph.errors import NodeCancelledError
from langgraph.types import Command

from app.core.config import get_settings
from app.core.contracts import (
    AgentLoader,
    KnowledgeGateway,
    ModelGateway,
    SkillGateway,
    ToolGateway,
    UserContext,
)
from app.core.logging import get_logger
from app.modules.runtime.checkpoint import get_checkpointer
from app.modules.runtime.context import ExecutionSink
from app.modules.runtime.service import RuntimeResult
from app.modules.workflow.engine import WorkflowState, build_workflow_graph, json_safe
from app.modules.workflow.schemas import WorkflowSnapshot, validate_definition

logger = get_logger(__name__)


def _interrupt_payload(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


class WorkflowRuntime:
    def __init__(
        self,
        *,
        model_gateway: ModelGateway,
        tool_gateway: ToolGateway,
        knowledge_gateway: KnowledgeGateway | None = None,
        skill_gateway: SkillGateway | None = None,
        agent_loader: AgentLoader | None = None,
        sink: ExecutionSink,
        checkpointer: Any = None,
    ) -> None:
        self.model_gateway = model_gateway
        self.tool_gateway = tool_gateway
        self.knowledge_gateway = knowledge_gateway
        self.skill_gateway = skill_gateway
        self.agent_loader = agent_loader
        self.sink = sink
        self._checkpointer = checkpointer

    async def _resolve_checkpointer(self) -> Any:
        if self._checkpointer is not None:
            return self._checkpointer
        return await get_checkpointer()

    async def run(
        self,
        *,
        execution_id: str,
        snapshot: WorkflowSnapshot,
        user: UserContext,
        input_text: str = "",
        variables: dict[str, Any] | None = None,
    ) -> RuntimeResult:
        validation = validate_definition(snapshot.definition)
        if not validation.valid:
            return RuntimeResult(
                status="failed",
                error="invalid workflow definition: " + "; ".join(validation.errors),
            )
        state: WorkflowState = {
            "execution_id": execution_id,
            "variables": json_safe({"input": input_text, **(variables or {})}),
            "steps": [],
            "usage": {},
            "final_output": None,
            "error": None,
            "last_condition": None,
            "citations": [],
            "node_outputs": {},
            "last_step_output": None,
        }
        try:
            checkpointer = await self._resolve_checkpointer()
            graph = build_workflow_graph(
                snapshot=snapshot,
                model_gateway=self.model_gateway,
                tool_gateway=self.tool_gateway,
                knowledge_gateway=self.knowledge_gateway,
                skill_gateway=self.skill_gateway,
                agent_loader=self.agent_loader,
                sink=self.sink,
                user=user,
                execution_id=execution_id,
                checkpointer=checkpointer,
            )
            timeout = self._timeout()
            async with asyncio.timeout(timeout):
                result = await graph.ainvoke(state, self._config(execution_id, snapshot))
        except TimeoutError:
            return RuntimeResult(
                status="failed",
                error=f"workflow execution timed out after {self._timeout()}s",
            )
        except NodeCancelledError as exc:
            raise asyncio.CancelledError from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - graph failures become failed executions
            logger.exception("workflow execution failed", extra={"execution_id": execution_id})
            return RuntimeResult(status="failed", error=f"workflow error: {exc}")
        return await self._result_from_state(result, snapshot, execution_id)

    async def resume(
        self,
        *,
        execution_id: str,
        snapshot: WorkflowSnapshot,
        user: UserContext,
        decision: dict[str, Any] | None = None,
        interrupt_id: str | None = None,
    ) -> RuntimeResult:
        try:
            checkpointer = await self._resolve_checkpointer()
            graph = build_workflow_graph(
                snapshot=snapshot,
                model_gateway=self.model_gateway,
                tool_gateway=self.tool_gateway,
                knowledge_gateway=self.knowledge_gateway,
                skill_gateway=self.skill_gateway,
                agent_loader=self.agent_loader,
                sink=self.sink,
                user=user,
                execution_id=execution_id,
                checkpointer=checkpointer,
            )
            config = self._config(execution_id, snapshot)
            if decision is None and interrupt_id is None:
                graph_input: Any = None
            elif interrupt_id:
                graph_input = Command(resume={interrupt_id: decision})
            else:
                graph_input = Command(resume=decision)
            timeout = self._timeout()
            async with asyncio.timeout(timeout):
                result = await graph.ainvoke(graph_input, config)
        except TimeoutError:
            return RuntimeResult(
                status="failed",
                error=f"workflow execution timed out after {self._timeout()}s",
            )
        except NodeCancelledError as exc:
            raise asyncio.CancelledError from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("workflow resume failed", extra={"execution_id": execution_id})
            return RuntimeResult(status="failed", error=f"workflow error: {exc}")
        return await self._result_from_state(result, snapshot, execution_id)

    @staticmethod
    def _config(execution_id: str, snapshot: WorkflowSnapshot) -> dict[str, Any]:
        return {
            "configurable": {"thread_id": execution_id},
            "recursion_limit": max(25, len(snapshot.definition.nodes) * 4 + 10),
        }

    @staticmethod
    def _timeout() -> int:
        settings = get_settings()
        return max(1, int(settings.execution_timeout_seconds or 600))

    async def _result_from_state(
        self, result: Any, snapshot: WorkflowSnapshot, execution_id: str
    ) -> RuntimeResult:
        state: dict[str, Any] = result if isinstance(result, dict) else {}
        interrupts = state.get("__interrupt__") or []
        usage = dict(state.get("usage") or {})
        payload = {
            "workflow_id": snapshot.workflow_id,
            "workflow_version": snapshot.version,
            "node_outputs": state.get("node_outputs") or {},
            "citations": state.get("citations") or [],
            "steps": state.get("steps") or [],
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
                interrupt_payload = _interrupt_payload(value)
                approval_id = interrupt_payload.get("approval_id")
                if approval_id and interrupt_id:
                    interrupt_map[str(approval_id)] = str(interrupt_id)
                if approval_id:
                    approval_ids.append(str(approval_id))
            if interrupt_map:
                await self.sink.attach_interrupt_ids(interrupt_map)
            return RuntimeResult(
                status="waiting_approval",
                output_text=None,
                output_payload=payload,
                usage=usage,
                approval_ids=approval_ids,
            )
        error = state.get("error")
        if error:
            return RuntimeResult(
                status="failed",
                output_text=None,
                output_payload=payload,
                usage=usage,
                error=str(error),
            )
        final_output = state.get("final_output")
        if final_output is None:
            return RuntimeResult(
                status="failed",
                output_text=None,
                output_payload=payload,
                usage=usage,
                error="workflow finished without producing an output",
            )
        return RuntimeResult(
            status="completed",
            output_text=str(final_output),
            output_payload=payload,
            usage=usage,
        )


__all__ = ["WorkflowRuntime"]
