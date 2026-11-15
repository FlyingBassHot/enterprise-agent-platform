from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any

import pytest

from app.core.contracts import (
    AgentSnapshot,
    ChatMessage,
    ChatResult,
    ModelConfigResolver,
    RetrievalChunk,
    ToolCall,
    ToolDescriptor,
    ToolExecutionResult,
    ToolInvocation,
    ToolSpec,
    Usage,
    UserContext,
)
from app.modules.runtime.checkpoint import close_checkpointer, create_checkpointer
from app.modules.runtime.context import ApprovalHandle, LLMCallRecord
from app.modules.workflow.engine import evaluate_condition, render_text
from app.modules.workflow.executor import WorkflowRuntime
from app.modules.workflow.schemas import (
    RetryPolicy,
    WorkflowDefinition,
    WorkflowNode,
    WorkflowSnapshot,
)

USER = UserContext(
    user_id="11111111-1111-1111-1111-111111111111",
    username="tester",
    display_name="Tester",
    permissions={"workflows:read", "workflows:write", "executions:write"},
)


def make_snapshot(
    definition: WorkflowDefinition,
    *,
    workflow_id: str = "wf-11111111",
    version: int = 1,
) -> WorkflowSnapshot:
    return WorkflowSnapshot(
        workflow_id=workflow_id,
        workflow_name="Test Workflow",
        owner_id=USER.user_id,
        version_id=f"ver-{version}",
        version=version,
        definition=definition,
    )


def make_tool(name: str = "echo", risk_level: str = "low", **overrides: Any) -> ToolDescriptor:
    base: dict[str, Any] = {
        "name": name,
        "description": f"{name} tool",
        "input_schema": {"type": "object", "properties": {}},
        "risk_level": risk_level,
        "timeout_seconds": 5,
    }
    base.update(overrides)
    return ToolDescriptor(**base)


def make_agent(**overrides: Any) -> AgentSnapshot:
    base: dict[str, Any] = {
        "agent_id": "agent-1",
        "agent_name": "Helper",
        "version_id": "av-1",
        "version": 1,
        "owner_id": USER.user_id,
        "system_prompt": "You are a deterministic helper.",
        "tool_names": [],
        "max_iterations": 6,
        "timeout_seconds": 30,
    }
    base.update(overrides)
    return AgentSnapshot(**base)


class FakeSink:
    def __init__(self, approvals: dict[str, dict[str, Any]] | None = None) -> None:
        self.events: list[dict[str, Any]] = []
        self.llm_calls: list[LLMCallRecord] = []
        self.approvals: dict[str, dict[str, Any]] = approvals if approvals is not None else {}
        self.interrupt_ids: dict[str, str] = {}
        self._counter = 0

    async def emit(self, event_type: str, payload: dict[str, Any] | None = None) -> int:
        self.events.append({"type": event_type, "payload": payload or {}})
        self._counter += 1
        return self._counter

    async def record_llm_call(self, record: LLMCallRecord) -> None:
        self.llm_calls.append(record)

    async def request_approval(
        self,
        *,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        risk_level: str,
        kind: str = "tool_call",
        title: str | None = None,
    ) -> ApprovalHandle:
        for approval_id, row in self.approvals.items():
            if row["tool_call_id"] == tool_call_id:
                return ApprovalHandle(id=approval_id, created=False)
        approval_id = f"approval-{len(self.approvals) + 1}"
        self.approvals[approval_id] = {
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "arguments": arguments,
            "risk_level": risk_level,
            "kind": kind,
            "title": title,
            "status": "pending",
        }
        return ApprovalHandle(id=approval_id, created=True)

    async def attach_interrupt_ids(self, mapping: dict[str, str]) -> None:
        self.interrupt_ids.update(mapping)

    def types(self) -> list[str]:
        return [event["type"] for event in self.events]

    def payloads(self, event_type: str) -> list[dict[str, Any]]:
        return [event["payload"] for event in self.events if event["type"] == event_type]


class FakeModel:
    def __init__(
        self,
        script: Sequence[dict[str, Any]] | None = None,
        *,
        failures: int = 0,
        cancel: bool = False,
    ) -> None:
        self.script = list(script or [])
        self.failures = failures
        self.cancel = cancel
        self.calls: list[dict[str, Any]] = []

    async def resolve(self, model_config_id: str | None) -> ModelConfigResolver:
        return ModelConfigResolver(
            config_id=model_config_id or "model-config-1",
            provider_type="mock",
            provider_name="fake-provider",
            model="fake-model",
        )

    async def chat(
        self,
        *,
        model_config_id: str | None,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec] = (),
        temperature: float | None = None,
        max_tokens: int | None = None,
        execution_id: str | None = None,
    ) -> ChatResult:
        self.calls.append(
            {
                "messages": list(messages),
                "tools": [tool.name for tool in tools],
                "model_config_id": model_config_id,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "execution_id": execution_id,
            }
        )
        if self.cancel:
            raise asyncio.CancelledError
        if self.failures:
            self.failures -= 1
            raise RuntimeError("transient model failure")
        message = self._compose(messages)
        return ChatResult(
            message=message,
            usage=Usage(prompt_tokens=3, completion_tokens=2, total_tokens=5),
        )

    async def health(self, model_config_id: str) -> tuple[bool, str]:
        return True, "ok"

    def _compose(self, messages: Sequence[ChatMessage]) -> ChatMessage:
        assistant_count = sum(1 for message in messages if message.role == "assistant")
        if self.script:
            step = self.script[min(assistant_count, len(self.script) - 1)]
        else:
            last_user = next(
                (message.content for message in reversed(messages) if message.role == "user"),
                "",
            )
            step = {"content": f"echo:{last_user}"}
        calls = [
            ToolCall(
                id=raw.get("id") or f"call_{assistant_count}_{position}",
                name=raw["name"],
                arguments=raw.get("arguments") or {},
            )
            for position, raw in enumerate(step.get("tool_calls") or [])
        ]
        return ChatMessage(role="assistant", content=step.get("content", ""), tool_calls=calls)


class FakeTools:
    def __init__(
        self,
        descriptors: Sequence[ToolDescriptor] = (),
        *,
        failures: int = 0,
        output: Any = None,
    ) -> None:
        self.descriptors = {descriptor.name: descriptor for descriptor in descriptors}
        self.failures = failures
        self.output = output
        self.invocations: list[ToolInvocation] = []

    async def describe(self, names: Sequence[str], user: UserContext) -> list[ToolDescriptor]:
        return [self.descriptors[name] for name in names if name in self.descriptors]

    async def execute(self, invocation: ToolInvocation, user: UserContext) -> ToolExecutionResult:
        self.invocations.append(invocation)
        if self.failures:
            self.failures -= 1
            raise RuntimeError("transient tool failure")
        output = self.output
        if output is None:
            output = {"tool": invocation.tool_name, "arguments": invocation.arguments}
        return ToolExecutionResult(ok=True, status="success", output=output, duration_ms=1)


class FakeKnowledge:
    def __init__(self, chunks: Sequence[RetrievalChunk] = ()) -> None:
        self.chunks = list(chunks)
        self.queries: list[dict[str, Any]] = []

    async def search(
        self,
        *,
        knowledge_base_ids: Sequence[str],
        query: str,
        user: UserContext,
        top_k: int = 5,
        metadata_filter: dict[str, Any] | None = None,
    ) -> list[RetrievalChunk]:
        self.queries.append(
            {
                "knowledge_base_ids": list(knowledge_base_ids),
                "query": query,
                "top_k": top_k,
                "metadata_filter": metadata_filter,
            }
        )
        return self.chunks[:top_k]


class FakeLoader:
    def __init__(self, snapshot: AgentSnapshot) -> None:
        self.snapshot = snapshot
        self.loaded: list[str] = []

    async def load_published(self, agent_id: str) -> AgentSnapshot:
        self.loaded.append(agent_id)
        return self.snapshot


@pytest.fixture
async def checkpointer(tmp_path):
    saver = await create_checkpointer(str(tmp_path / "workflow.db"))
    yield saver
    await close_checkpointer(saver)


def make_runtime(
    *,
    model: FakeModel | None = None,
    tools: FakeTools | None = None,
    knowledge: FakeKnowledge | None = None,
    loader: FakeLoader | None = None,
    sink: FakeSink | None = None,
    checkpointer: Any = None,
) -> WorkflowRuntime:
    return WorkflowRuntime(
        model_gateway=model or FakeModel(),
        tool_gateway=tools or FakeTools(),
        knowledge_gateway=knowledge,
        agent_loader=loader,
        sink=sink or FakeSink(),
        checkpointer=checkpointer,
    )


# ---------------------------------------------------------------------------
# Templates and conditions
# ---------------------------------------------------------------------------


def test_render_and_condition_helpers() -> None:
    variables = {"name": "Ada", "user": {"score": 12}, "tags": ["x", "y"], "flag": False}
    assert render_text("Hello {{ name }} / {{ user.score }}", variables) == "Hello Ada / 12"
    assert render_text("{{ missing }}", variables) == "{{ missing }}"
    assert render_text("{{ tags }}", variables) == '["x", "y"]'
    assert evaluate_condition("user.score >= 10", variables) is True
    assert evaluate_condition("not user.score < 10", variables) is True
    assert evaluate_condition("tags contains 'x'", variables) is True
    assert evaluate_condition("flag == false", variables) is True
    assert evaluate_condition("name == 'Grace'", variables) is False
    with pytest.raises(ValueError):
        evaluate_condition("no operator here", variables)


# ---------------------------------------------------------------------------
# Linear + condition
# ---------------------------------------------------------------------------


async def test_linear_start_llm_end(checkpointer) -> None:
    definition = WorkflowDefinition(
        nodes=[
            WorkflowNode(id="start", type="start", next="gen"),
            WorkflowNode(
                id="gen",
                type="llm",
                name="Generate",
                config={"prompt": "Hello {{ name }}", "output_key": "llm_output"},
                next="end",
            ),
            WorkflowNode(id="end", type="end"),
        ],
        output="{{ llm_output }}",
    )
    sink = FakeSink()
    model = FakeModel()
    runtime = make_runtime(model=model, sink=sink, checkpointer=checkpointer)

    result = await runtime.run(
        execution_id="exec-linear",
        snapshot=make_snapshot(definition),
        user=USER,
        input_text="hi",
        variables={"name": "Ada"},
    )

    assert result.status == "completed"
    assert result.output_text == "echo:Hello Ada"
    assert result.usage == {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
    assert result.output_payload["workflow_id"] == "wf-11111111"
    assert result.output_payload["workflow_version"] == 1
    assert set(result.output_payload["node_outputs"]) == {"start", "gen", "end"}
    assert model.calls[0]["messages"][-1].content == "Hello Ada"
    types = sink.types()
    assert types.count("workflow_node_started") == 3
    assert types.count("workflow_node_completed") == 3
    assert "message_completed" in types
    assert sink.llm_calls[0].provider == "fake-provider"


async def _run_condition(branch: str, variables: dict[str, Any], checkpointer: Any) -> str:
    definition = WorkflowDefinition(
        nodes=[
            WorkflowNode(id="start", type="start", next="check"),
            WorkflowNode(
                id="check",
                type="condition",
                config={"expression": "user.score >= 10"},
                on_true="high",
                on_false="low",
            ),
            WorkflowNode(
                id="high",
                type="llm",
                config={"prompt": "HIGH", "output_key": "llm_output"},
                next="end",
            ),
            WorkflowNode(
                id="low",
                type="llm",
                config={"prompt": "LOW", "output_key": "llm_output"},
                next="end",
            ),
            WorkflowNode(id="end", type="end"),
        ],
        output="{{ llm_output }}",
    )
    sink = FakeSink()
    model = FakeModel([{"content": branch}])
    runtime = make_runtime(model=model, sink=sink, checkpointer=checkpointer)
    result = await runtime.run(
        execution_id=f"exec-condition-{branch}",
        snapshot=make_snapshot(definition, workflow_id=f"wf-{branch}"),
        user=USER,
        variables=variables,
    )
    assert result.status == "completed"
    run_nodes = [step["node_id"] for step in result.output_payload["steps"]]
    assert ("high" in run_nodes) is (branch == "HIGH")
    assert ("low" in run_nodes) is (branch == "LOW")
    return result.output_text or ""


async def test_condition_routes_true_and_false(checkpointer) -> None:
    assert await _run_condition("HIGH", {"user": {"score": 12}}, checkpointer) == "HIGH"
    assert await _run_condition("LOW", {"user": {"score": 3}}, checkpointer) == "LOW"


# ---------------------------------------------------------------------------
# Tool node
# ---------------------------------------------------------------------------


async def test_tool_node_executes_and_renders_arguments(checkpointer) -> None:
    definition = WorkflowDefinition(
        nodes=[
            WorkflowNode(id="start", type="start", next="call"),
            WorkflowNode(
                id="call",
                type="tool",
                config={
                    "tool_name": "echo",
                    "arguments": {"value": "{{ topic }}", "nested": {"count": 2}},
                    "output_key": "tool_output",
                },
                next="end",
            ),
            WorkflowNode(id="end", type="end"),
        ],
        output="{{ tool_output }}",
    )
    sink = FakeSink()
    tools = FakeTools([make_tool("echo")])
    runtime = make_runtime(tools=tools, sink=sink, checkpointer=checkpointer)

    result = await runtime.run(
        execution_id="exec-tool",
        snapshot=make_snapshot(definition),
        user=USER,
        variables={"topic": "alpha"},
    )

    assert result.status == "completed"
    assert len(tools.invocations) == 1
    assert tools.invocations[0].approved is True
    assert tools.invocations[0].arguments == {"value": "alpha", "nested": {"count": 2}}
    assert "alpha" in (result.output_text or "")
    types = sink.types()
    assert "tool_call_started" in types
    assert "tool_call_completed" in types
    assert sink.payloads("tool_call_completed")[0]["ok"] is True


async def test_high_risk_tool_pauses_for_approval_and_resumes(checkpointer) -> None:
    definition = WorkflowDefinition(
        nodes=[
            WorkflowNode(id="start", type="start", next="call"),
            WorkflowNode(
                id="call",
                type="tool",
                config={"tool_name": "wire_money", "arguments": {"amount": 1000}},
                next="end",
            ),
            WorkflowNode(id="end", type="end"),
        ],
        output="{{ tool_output }}",
    )
    sink = FakeSink()
    tools = FakeTools([make_tool("wire_money", "high", requires_approval=True)])
    runtime = make_runtime(tools=tools, sink=sink, checkpointer=checkpointer)

    paused = await runtime.run(
        execution_id="exec-tool-approval",
        snapshot=make_snapshot(definition),
        user=USER,
    )
    assert paused.status == "waiting_approval"
    assert paused.approval_ids == ["approval-1"]
    assert "approval_required" in sink.types()
    assert tools.invocations == []
    interrupt_id = sink.interrupt_ids[paused.approval_ids[0]]

    resumed_sink = FakeSink(approvals=sink.approvals)
    resumed_runtime = make_runtime(tools=tools, sink=resumed_sink, checkpointer=checkpointer)
    resumed = await resumed_runtime.resume(
        execution_id="exec-tool-approval",
        snapshot=make_snapshot(definition),
        user=USER,
        decision={"approved": True, "comment": "ok"},
        interrupt_id=interrupt_id,
    )

    assert resumed.status == "completed"
    assert len(tools.invocations) == 1
    assert tools.invocations[0].approved is True
    assert "approval_resolved" in resumed_sink.types()
    assert resumed_sink.payloads("approval_resolved")[0]["approved"] is True


# ---------------------------------------------------------------------------
# Approval node
# ---------------------------------------------------------------------------


def _approval_definition(on_reject: str = "fail") -> WorkflowDefinition:
    return WorkflowDefinition(
        nodes=[
            WorkflowNode(id="start", type="start", next="approve"),
            WorkflowNode(
                id="approve",
                type="approval",
                name="Approve deploy",
                config={
                    "title": "Approve deploy",
                    "description": "Deploy to production",
                    "on_reject": on_reject,
                },
                next="end",
            ),
            WorkflowNode(id="end", type="end"),
        ],
        output="{{ approve }}",
    )


async def test_approval_node_pauses_and_approves(checkpointer) -> None:
    definition = _approval_definition()
    sink = FakeSink()
    runtime = make_runtime(sink=sink, checkpointer=checkpointer)

    paused = await runtime.run(
        execution_id="exec-approval-node",
        snapshot=make_snapshot(definition),
        user=USER,
    )
    assert paused.status == "waiting_approval"
    assert paused.approval_ids == ["approval-1"]
    assert sink.approvals["approval-1"]["kind"] == "workflow_node"
    interrupt_id = sink.interrupt_ids["approval-1"]

    resumed_sink = FakeSink(approvals=sink.approvals)
    resumed_runtime = make_runtime(sink=resumed_sink, checkpointer=checkpointer)
    resumed = await resumed_runtime.resume(
        execution_id="exec-approval-node",
        snapshot=make_snapshot(definition),
        user=USER,
        decision={"approved": True, "comment": "ship it"},
        interrupt_id=interrupt_id,
    )

    assert resumed.status == "completed"
    assert resumed.output_text is not None
    assert json.loads(resumed.output_text)["approved"] is True
    assert "approval_resolved" in resumed_sink.types()


async def test_approval_node_reject_fails(checkpointer) -> None:
    definition = _approval_definition("fail")
    sink = FakeSink()
    runtime = make_runtime(sink=sink, checkpointer=checkpointer)
    paused = await runtime.run(
        execution_id="exec-approval-fail",
        snapshot=make_snapshot(definition),
        user=USER,
    )
    assert paused.status == "waiting_approval"

    resumed = await runtime.resume(
        execution_id="exec-approval-fail",
        snapshot=make_snapshot(definition),
        user=USER,
        decision={"approved": False, "comment": "not now"},
        interrupt_id=sink.interrupt_ids["approval-1"],
    )

    assert resumed.status == "failed"
    assert resumed.error is not None
    assert "rejected" in resumed.error
    assert "workflow_node_failed" in sink.types()


async def test_approval_node_reject_continue(checkpointer) -> None:
    definition = _approval_definition("continue")
    sink = FakeSink()
    runtime = make_runtime(sink=sink, checkpointer=checkpointer)
    paused = await runtime.run(
        execution_id="exec-approval-continue",
        snapshot=make_snapshot(definition),
        user=USER,
    )
    assert paused.status == "waiting_approval"

    resumed = await runtime.resume(
        execution_id="exec-approval-continue",
        snapshot=make_snapshot(definition),
        user=USER,
        decision={"approved": False, "comment": "skip"},
        interrupt_id=sink.interrupt_ids["approval-1"],
    )

    assert resumed.status == "completed"
    assert resumed.output_text is not None
    assert json.loads(resumed.output_text)["approved"] is False
    step = next(item for item in resumed.output_payload["steps"] if item["node_id"] == "approve")
    assert step["status"] == "rejected"


# ---------------------------------------------------------------------------
# Knowledge node
# ---------------------------------------------------------------------------


async def test_knowledge_node_records_citations(checkpointer) -> None:
    chunks = [
        RetrievalChunk(
            chunk_id="c1",
            document_id="d1",
            document_title="Runbook",
            knowledge_base_id="kb-1",
            content="Restart the service.",
            score=0.9,
        ),
        RetrievalChunk(
            chunk_id="c2",
            document_id="d2",
            document_title="FAQ",
            knowledge_base_id="kb-1",
            content="Use the status page.",
            score=0.5,
        ),
    ]
    definition = WorkflowDefinition(
        nodes=[
            WorkflowNode(id="start", type="start", next="lookup"),
            WorkflowNode(
                id="lookup",
                type="knowledge",
                config={
                    "knowledge_base_ids": ["kb-1"],
                    "query": "How do I fix {{ topic }}?",
                    "top_k": 2,
                    "output_key": "knowledge",
                },
                next="end",
            ),
            WorkflowNode(id="end", type="end"),
        ],
        output="{{ knowledge }}",
    )
    sink = FakeSink()
    knowledge = FakeKnowledge(chunks)
    runtime = make_runtime(knowledge=knowledge, sink=sink, checkpointer=checkpointer)

    result = await runtime.run(
        execution_id="exec-knowledge",
        snapshot=make_snapshot(definition),
        user=USER,
        variables={"topic": "billing"},
    )

    assert result.status == "completed"
    assert knowledge.queries[0]["query"] == "How do I fix billing?"
    assert knowledge.queries[0]["top_k"] == 2
    assert len(result.output_payload["citations"]) == 2
    assert result.output_payload["citations"][0]["document_title"] == "Runbook"
    assert "Restart the service." in (result.output_text or "")
    event = sink.payloads("knowledge_retrieved")[0]
    assert event["count"] == 2


# ---------------------------------------------------------------------------
# Agent node
# ---------------------------------------------------------------------------


def _agent_definition(agent_id: str = "agent-1") -> WorkflowDefinition:
    return WorkflowDefinition(
        nodes=[
            WorkflowNode(id="start", type="start", next="delegate"),
            WorkflowNode(
                id="delegate",
                type="agent",
                config={
                    "agent_id": agent_id,
                    "input": "Handle {{ topic }}",
                    "output_key": "agent_output",
                    "max_iterations": 4,
                },
                next="end",
            ),
            WorkflowNode(id="end", type="end"),
        ],
        output="{{ agent_output }}",
    )


async def test_agent_node_runs_low_risk_tool_loop(checkpointer) -> None:
    sink = FakeSink()
    tools = FakeTools([make_tool("echo", "low")])
    model = FakeModel(
        [
            {"tool_calls": [{"name": "echo", "arguments": {"value": "from-agent"}}]},
            {"content": "agent final"},
        ]
    )
    loader = FakeLoader(make_agent(tool_names=["echo"]))
    runtime = make_runtime(
        model=model, tools=tools, loader=loader, sink=sink, checkpointer=checkpointer
    )

    result = await runtime.run(
        execution_id="exec-agent",
        snapshot=make_snapshot(_agent_definition()),
        user=USER,
        variables={"topic": "billing"},
    )

    assert result.status == "completed"
    assert result.output_text == "agent final"
    assert loader.loaded == ["agent-1"]
    assert len(tools.invocations) == 1
    assert tools.invocations[0].approved is False
    assert tools.invocations[0].arguments == {"value": "from-agent"}
    assert model.calls[0]["tools"] == ["echo"]
    assert model.calls[0]["messages"][-1].content == "Handle billing"
    types = sink.types()
    assert types.count("message_completed") == 2
    assert "tool_call_started" in types and "tool_call_completed" in types
    assert len(sink.llm_calls) == 2


async def test_agent_node_rejects_high_risk_tools(checkpointer) -> None:
    sink = FakeSink()
    tools = FakeTools([make_tool("wire_money", "high")])
    loader = FakeLoader(make_agent(tool_names=["wire_money"]))
    runtime = make_runtime(tools=tools, loader=loader, sink=sink, checkpointer=checkpointer)

    result = await runtime.run(
        execution_id="exec-agent-blocked",
        snapshot=make_snapshot(_agent_definition()),
        user=USER,
    )

    assert result.status == "failed"
    assert result.error is not None
    assert "require approval" in result.error
    assert tools.invocations == []


# ---------------------------------------------------------------------------
# Retry, validation, cancellation
# ---------------------------------------------------------------------------


async def test_retry_succeeds_after_transient_failure(checkpointer) -> None:
    definition = WorkflowDefinition(
        nodes=[
            WorkflowNode(id="start", type="start", next="gen"),
            WorkflowNode(
                id="gen",
                type="llm",
                config={"prompt": "retry me", "output_key": "llm_output"},
                next="end",
                retry=RetryPolicy(max_attempts=2, backoff_seconds=0),
            ),
            WorkflowNode(id="end", type="end"),
        ],
        output="{{ llm_output }}",
    )
    sink = FakeSink()
    model = FakeModel([{"content": "recovered"}], failures=1)
    runtime = make_runtime(model=model, sink=sink, checkpointer=checkpointer)

    result = await runtime.run(
        execution_id="exec-retry",
        snapshot=make_snapshot(definition),
        user=USER,
    )

    assert result.status == "completed"
    assert result.output_text == "recovered"
    assert len(model.calls) == 2
    assert [record.status for record in sink.llm_calls] == ["failed", "success"]


async def test_invalid_definition_rejected_before_graph(checkpointer) -> None:
    definition = WorkflowDefinition(
        nodes=[
            WorkflowNode(id="start", type="start"),
            WorkflowNode(id="end", type="end"),
        ]
    )
    model = FakeModel()
    sink = FakeSink()
    runtime = make_runtime(model=model, sink=sink, checkpointer=checkpointer)

    result = await runtime.run(
        execution_id="exec-invalid",
        snapshot=make_snapshot(definition),
        user=USER,
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.startswith("invalid workflow definition")
    assert model.calls == []
    assert sink.events == []


async def test_cancellation_propagates(checkpointer) -> None:
    definition = WorkflowDefinition(
        nodes=[
            WorkflowNode(id="start", type="start", next="gen"),
            WorkflowNode(
                id="gen",
                type="llm",
                config={"prompt": "cancel me", "output_key": "llm_output"},
                next="end",
            ),
            WorkflowNode(id="end", type="end"),
        ],
        output="{{ llm_output }}",
    )
    runtime = make_runtime(model=FakeModel(cancel=True), sink=FakeSink(), checkpointer=checkpointer)

    with pytest.raises(asyncio.CancelledError):
        await runtime.run(
            execution_id="exec-cancel",
            snapshot=make_snapshot(definition),
            user=USER,
        )
