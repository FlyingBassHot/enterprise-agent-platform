from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest

import app.modules.execution.models  # noqa: F401  (register tables before app lifespan)
from app.core.contracts import (
    AgentSnapshot,
    ChatMessage,
    ChatResult,
    ModelConfigResolver,
    ModelStreamEvent,
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
from app.modules.runtime.service import RuntimeService

USER = UserContext(
    user_id="11111111-1111-1111-1111-111111111111",
    username="tester",
    display_name="Tester",
    permissions={"agents:write", "executions:write", "executions:read", "approvals:write"},
)


def make_snapshot(**overrides: Any) -> AgentSnapshot:
    base: dict[str, Any] = {
        "agent_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "agent_name": "Test Agent",
        "version_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        "version": 1,
        "owner_id": USER.user_id,
        "system_prompt": "You are a deterministic test agent.",
        "max_iterations": 6,
        "timeout_seconds": 30,
        "approval_risk_levels": ["high", "critical"],
    }
    base.update(overrides)
    return AgentSnapshot(**base)


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
        approval_key: str | None = None,
    ) -> ApprovalHandle:
        for approval_id, row in self.approvals.items():
            if (row.get("approval_key") or row["tool_call_id"]) == (
                approval_key or tool_call_id
            ):
                return ApprovalHandle(id=approval_id, created=False)
        approval_id = f"approval-{len(self.approvals) + 1}"
        self.approvals[approval_id] = {
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "arguments": arguments,
            "risk_level": risk_level,
            "status": "pending",
            "approval_key": approval_key,
            "kind": kind,
            "title": title,
        }
        return ApprovalHandle(id=approval_id, created=True)

    async def attach_interrupt_ids(self, mapping: dict[str, str]) -> None:
        self.interrupt_ids.update(mapping)

    def types(self) -> list[str]:
        return [event["type"] for event in self.events]


class FakeModel:
    """Deterministic scripted model. Consumes steps by assistant-message count."""

    def __init__(self, script: Sequence[dict[str, Any]], *, delay: float = 0.0) -> None:
        self.script = list(script)
        self.delay = delay
        self.calls: list[dict[str, Any]] = []

    async def resolve(self, model_config_id: str | None) -> ModelConfigResolver:
        return ModelConfigResolver(
            config_id=model_config_id or "cccccccc-cccc-cccc-cccc-cccccccccccc",
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
        message = self._compose(messages)
        return ChatResult(message=message, usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2))

    def stream_chat(
        self,
        *,
        model_config_id: str | None,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec] = (),
        temperature: float | None = None,
        max_tokens: int | None = None,
        execution_id: str | None = None,
    ) -> AsyncIterator[ModelStreamEvent]:
        self.calls.append(
            {
                "messages": list(messages),
                "tools": [tool.name for tool in tools],
                "execution_id": execution_id,
            }
        )

        async def generator() -> AsyncIterator[ModelStreamEvent]:
            if self.delay:
                await asyncio.sleep(self.delay)
            message = self._compose(messages)
            content = message.content or ""
            for index in range(0, len(content), 5):
                yield ModelStreamEvent(kind="delta", content=content[index : index + 5])
            for call in message.tool_calls:
                yield ModelStreamEvent(kind="tool_call", tool_call=call)
            yield ModelStreamEvent(
                kind="done",
                finish_reason="tool_calls" if message.tool_calls else "stop",
                usage=Usage(prompt_tokens=7, completion_tokens=3, total_tokens=10),
            )

        return generator()

    def _compose(self, messages: Sequence[ChatMessage]) -> ChatMessage:
        index = sum(1 for message in messages if message.role == "assistant")
        step = self.script[min(index, len(self.script) - 1)] if self.script else {"content": ""}
        calls = [
            ToolCall(
                id=raw.get("id") or f"call_{index}_{position}",
                name=raw["name"],
                arguments=raw.get("arguments") or {},
            )
            for position, raw in enumerate(step.get("tool_calls") or [])
        ]
        return ChatMessage(role="assistant", content=step.get("content", ""), tool_calls=calls)

    async def health(self, model_config_id: str) -> tuple[bool, str]:
        return True, "ok"


class FakeTools:
    def __init__(self, descriptors: Sequence[ToolDescriptor] = ()) -> None:
        self.descriptors = {descriptor.name: descriptor for descriptor in descriptors}
        self.invocations: list[ToolInvocation] = []

    async def describe(
        self, names: Sequence[str], user: UserContext
    ) -> list[ToolDescriptor]:
        return [self.descriptors[name] for name in names if name in self.descriptors]

    async def execute(
        self, invocation: ToolInvocation, user: UserContext
    ) -> ToolExecutionResult:
        self.invocations.append(invocation)
        return ToolExecutionResult(
            ok=True, status="success", output={"echo": invocation.arguments}, duration_ms=2
        )


class FakeLoader:
    def __init__(self, snapshot: AgentSnapshot) -> None:
        self.snapshot = snapshot

    async def load_published(self, agent_id: str) -> AgentSnapshot:
        return self.snapshot


@pytest.fixture
async def checkpointer(tmp_path):
    saver = await create_checkpointer(str(tmp_path / "checkpoints.db"))
    yield saver
    await close_checkpointer(saver)


async def test_agent_completes_without_tools(checkpointer) -> None:
    sink = FakeSink()
    model = FakeModel([{"content": "hello world"}])
    runtime = RuntimeService(
        model_gateway=model,
        tool_gateway=FakeTools(),
        sink=sink,
        checkpointer=checkpointer,
    )

    result = await runtime.run(
        execution_id="exec-no-tools",
        snapshot=make_snapshot(),
        user=USER,
        user_text="hi there",
    )

    assert result.status == "completed"
    assert result.output_text == "hello world"
    assert result.usage == {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
    assert sink.types()[0] == "message_delta"
    assert sink.types()[-1] == "message_completed"
    assert sink.events[-1]["payload"]["content"] == "hello world"
    assert len(sink.llm_calls) == 1
    assert sink.llm_calls[0].provider == "fake-provider"
    assert sink.llm_calls[0].status == "success"
    assert model.calls[0]["messages"][0].role == "system"
    assert model.calls[0]["messages"][-1].content == "hi there"


async def test_low_risk_tool_call_executes_and_answers(checkpointer) -> None:
    sink = FakeSink()
    tools = FakeTools([make_tool("echo", "low")])
    model = FakeModel(
        [
            {"tool_calls": [{"name": "echo", "arguments": {"value": 42}}]},
            {"content": "final answer"},
        ]
    )
    runtime = RuntimeService(
        model_gateway=model, tool_gateway=tools, sink=sink, checkpointer=checkpointer
    )

    result = await runtime.run(
        execution_id="exec-tool",
        snapshot=make_snapshot(tool_names=["echo"]),
        user=USER,
        user_text="use the tool",
    )

    assert result.status == "completed"
    assert result.output_text == "final answer"
    assert len(tools.invocations) == 1
    assert tools.invocations[0].approved is True
    assert tools.invocations[0].arguments == {"value": 42}
    types = sink.types()
    assert "tool_call_started" in types
    assert "tool_call_completed" in types
    tool_started = types.index("tool_call_started")
    assert "message_completed" in types[:tool_started]
    assert types.count("message_completed") == 2


async def test_high_risk_tool_waits_for_approval_and_resumes(checkpointer) -> None:
    sink = FakeSink()
    tools = FakeTools([make_tool("wire_money", "high", requires_approval=True)])
    model = FakeModel(
        [
            {"tool_calls": [{"name": "wire_money", "arguments": {"amount": 1000}}]},
            {"content": "transfer completed"},
        ]
    )
    runtime = RuntimeService(
        model_gateway=model, tool_gateway=tools, sink=sink, checkpointer=checkpointer
    )

    paused = await runtime.run(
        execution_id="exec-approval",
        snapshot=make_snapshot(tool_names=["wire_money"]),
        user=USER,
        user_text="send money",
    )

    assert paused.status == "waiting_approval"
    assert paused.approval_ids == ["approval-1"]
    assert "approval_required" in sink.types()
    assert tools.invocations == []
    approval_id = paused.approval_ids[0]
    interrupt_id = sink.interrupt_ids[approval_id]
    assert interrupt_id

    # A brand-new runtime instance on the same checkpoint file must resume.
    resumed_sink = FakeSink(approvals=sink.approvals)
    resumed_runtime = RuntimeService(
        model_gateway=model,
        tool_gateway=tools,
        sink=resumed_sink,
        checkpointer=checkpointer,
    )
    resumed = await resumed_runtime.resume(
        execution_id="exec-approval",
        user=USER,
        snapshot=make_snapshot(tool_names=["wire_money"]),
        decision={"approved": True, "comment": "looks fine"},
        interrupt_id=interrupt_id,
    )

    assert resumed.status == "completed"
    assert resumed.output_text == "transfer completed"
    assert len(tools.invocations) == 1
    assert tools.invocations[0].approved is True
    assert "approval_resolved" in resumed_sink.types()
    assert resumed_sink.events[0]["payload"]["approved"] is True


async def test_rejected_tool_returns_to_model(checkpointer) -> None:
    sink = FakeSink()
    tools = FakeTools([make_tool("wire_money", "high", requires_approval=True)])
    model = FakeModel(
        [
            {"tool_calls": [{"name": "wire_money", "arguments": {"amount": 5}}]},
            {"content": "understood, not transferring"},
        ]
    )
    runtime = RuntimeService(
        model_gateway=model, tool_gateway=tools, sink=sink, checkpointer=checkpointer
    )

    paused = await runtime.run(
        execution_id="exec-reject",
        snapshot=make_snapshot(tool_names=["wire_money"]),
        user=USER,
        user_text="send money",
    )
    assert paused.status == "waiting_approval"
    approval_id = paused.approval_ids[0]

    resumed = await runtime.resume(
        execution_id="exec-reject",
        user=USER,
        snapshot=make_snapshot(tool_names=["wire_money"]),
        decision={"approved": False, "comment": "too risky"},
        interrupt_id=sink.interrupt_ids[approval_id],
    )

    assert resumed.status == "completed"
    assert resumed.output_text == "understood, not transferring"
    assert tools.invocations == []
    last_call_messages = model.calls[-1]["messages"]
    tool_messages = [message for message in last_call_messages if message.role == "tool"]
    assert tool_messages
    rejection = json.loads(tool_messages[-1].content)
    assert rejection["status"] == "approval_rejected"
    assert rejection["error_code"] == "approval_rejected"
    assert rejection["retryable"] is False
    assert rejection["hint"]
    assert "too risky" in tool_messages[-1].content


async def test_resume_with_fresh_checkpointer_connection(tmp_path) -> None:
    path = str(tmp_path / "durable.db")
    saver = await create_checkpointer(path)
    sink = FakeSink()
    tools = FakeTools([make_tool("wire_money", "high")])
    model = FakeModel(
        [
            {"tool_calls": [{"name": "wire_money", "arguments": {"amount": 1}}]},
            {"content": "done"},
        ]
    )
    runtime = RuntimeService(
        model_gateway=model, tool_gateway=tools, sink=sink, checkpointer=saver
    )
    paused = await runtime.run(
        execution_id="exec-durable",
        snapshot=make_snapshot(tool_names=["wire_money"]),
        user=USER,
        user_text="go",
    )
    assert paused.status == "waiting_approval"
    await close_checkpointer(saver)

    fresh = await create_checkpointer(path)
    try:
        fresh_sink = FakeSink(approvals=sink.approvals)
        resumed_runtime = RuntimeService(
            model_gateway=model,
            tool_gateway=tools,
            sink=fresh_sink,
            checkpointer=fresh,
        )
        resumed = await resumed_runtime.resume(
            execution_id="exec-durable",
            user=USER,
            snapshot=make_snapshot(tool_names=["wire_money"]),
            decision={"approved": True, "comment": "ok"},
            interrupt_id=sink.interrupt_ids[paused.approval_ids[0]],
        )
        assert resumed.status == "completed"
        assert len(tools.invocations) == 1
    finally:
        await close_checkpointer(fresh)


async def test_max_iterations_reports_failure(checkpointer) -> None:
    sink = FakeSink()
    tools = FakeTools([make_tool("echo", "low")])
    model = FakeModel([{"tool_calls": [{"name": "echo", "arguments": {}}]}])
    runtime = RuntimeService(
        model_gateway=model, tool_gateway=tools, sink=sink, checkpointer=checkpointer
    )
    result = await runtime.run(
        execution_id="exec-loop",
        snapshot=make_snapshot(tool_names=["echo"], max_iterations=2),
        user=USER,
        user_text="loop",
    )
    assert result.status == "failed"
    assert "maximum iterations" in (result.error or "")
    assert len(model.calls) == 2


async def test_platform_risk_overrides_agent_approval_list(checkpointer) -> None:
    sink = FakeSink()
    tools = FakeTools([make_tool("wire_money", "high")])
    model = FakeModel(
        [
            {"tool_calls": [{"name": "wire_money", "arguments": {"amount": 10}}]},
            {"content": "done"},
        ]
    )
    runtime = RuntimeService(
        model_gateway=model, tool_gateway=tools, sink=sink, checkpointer=checkpointer
    )
    paused = await runtime.run(
        execution_id="exec-platform-approval",
        snapshot=make_snapshot(tool_names=["wire_money"], approval_risk_levels=[]),
        user=USER,
        user_text="pay",
    )
    assert paused.status == "waiting_approval"
    assert tools.invocations == []
    assert "approval_required" in sink.types()


async def test_unbound_tool_call_is_rejected_without_execution(checkpointer) -> None:
    sink = FakeSink()
    tools = FakeTools([make_tool("dangerous", "low")])
    model = FakeModel(
        [
            {"tool_calls": [{"name": "dangerous", "arguments": {}}]},
            {"content": "tool rejected"},
        ]
    )
    runtime = RuntimeService(
        model_gateway=model, tool_gateway=tools, sink=sink, checkpointer=checkpointer
    )
    result = await runtime.run(
        execution_id="exec-unbound-tool",
        snapshot=make_snapshot(tool_names=["allowed"]),
        user=USER,
        user_text="try a tool",
    )
    assert result.status == "completed"
    assert tools.invocations == []
    completed = [event for event in sink.events if event["type"] == "tool_call_completed"]
    assert completed and completed[0]["payload"]["status"] == "failed"
    assert "not bound" in completed[0]["payload"]["error"]


async def test_approval_arguments_are_masked(checkpointer) -> None:
    sink = FakeSink()
    tools = FakeTools(
        [make_tool("wire_money", "high", sensitive_fields=["token"])]
    )
    model = FakeModel(
        [
            {
                "tool_calls": [
                    {"name": "wire_money", "arguments": {"amount": 5, "token": "super-secret"}}
                ]
            },
            {"content": "waiting"},
        ]
    )
    runtime = RuntimeService(
        model_gateway=model, tool_gateway=tools, sink=sink, checkpointer=checkpointer
    )
    paused = await runtime.run(
        execution_id="exec-masked-approval",
        snapshot=make_snapshot(tool_names=["wire_money"]),
        user=USER,
        user_text="pay",
    )
    assert paused.status == "waiting_approval"
    approval = next(iter(sink.approvals.values()))
    assert approval["arguments"]["token"] == "***"
    assert approval["arguments"]["amount"] == 5
    required = next(
        event for event in sink.events if event["type"] == "approval_required"
    )
    assert required["payload"]["arguments"]["token"] == "***"

    resumed = await runtime.resume(
        execution_id="exec-masked-approval",
        user=USER,
        snapshot=make_snapshot(tool_names=["wire_money"]),
        decision={"approved": True, "comment": "ok"},
        interrupt_id=sink.interrupt_ids[paused.approval_ids[0]],
    )
    assert resumed.status == "completed"
    started = next(
        event for event in sink.events if event["type"] == "tool_call_started"
    )
    assert started["payload"]["arguments"]["token"] == "***"
    assert started["payload"]["arguments"]["amount"] == 5


class FakeKnowledge:
    def __init__(self, chunks):
        self.chunks = chunks
        self.calls = []

    async def search(
        self,
        *,
        knowledge_base_ids,
        query,
        user,
        top_k=5,
        metadata_filter=None,
    ):
        self.calls.append(
            {
                "knowledge_base_ids": list(knowledge_base_ids),
                "query": query,
                "top_k": top_k,
            }
        )
        return list(self.chunks)


class FakeSkills:
    def __init__(self, contexts):
        self.contexts = contexts

    async def resolve_for_agent(self, skill_names, user):
        return [context for context in self.contexts if context.name in set(skill_names)]


async def test_runtime_injects_knowledge_citations(checkpointer) -> None:
    from app.core.contracts import RetrievalChunk, SkillContext

    sink = FakeSink()
    tools = FakeTools()
    model = FakeModel([{"content": "answer with citation"}])
    knowledge = FakeKnowledge(
        [
            RetrievalChunk(
                chunk_id="chunk-1",
                document_id="doc-1",
                document_title="Audit Rules",
                knowledge_base_id="kb-1",
                content="Large payments over 100000 require review.",
                score=0.93,
            )
        ]
    )
    skills = FakeSkills(
        [
            SkillContext(
                name="audit-skill",
                version="1.0",
                instructions="Inspect duplicate payments first.",
            )
        ]
    )
    runtime = RuntimeService(
        model_gateway=model,
        tool_gateway=tools,
        sink=sink,
        knowledge_gateway=knowledge,
        skill_gateway=skills,
        checkpointer=checkpointer,
    )
    result = await runtime.run(
        execution_id="exec-knowledge",
        snapshot=make_snapshot(
            knowledge_base_ids=["kb-1"],
            skill_names=["audit-skill"],
            retrieval_enabled=True,
            retrieval_top_k=3,
        ),
        user=USER,
        user_text="what is the review threshold?",
    )
    assert result.status == "completed"
    assert knowledge.calls == [
        {
            "knowledge_base_ids": ["kb-1"],
            "query": "what is the review threshold?",
            "top_k": 3,
        }
    ]
    system_prompt = model.calls[0]["messages"][0].content
    assert "[K1]" in system_prompt
    assert "untrusted reference material" in system_prompt
    assert "Inspect duplicate payments first." in system_prompt
    retrieved = next(
        event for event in sink.events if event["type"] == "knowledge_retrieved"
    )
    assert retrieved["payload"]["citations"][0]["chunk_id"] == "chunk-1"
    assert result.output_payload["knowledge_citations"][0]["label"] == "K1"


async def test_runtime_skips_retrieval_when_disabled(checkpointer) -> None:
    sink = FakeSink()
    model = FakeModel([{"content": "plain"}])
    knowledge = FakeKnowledge([])
    runtime = RuntimeService(
        model_gateway=model,
        tool_gateway=FakeTools(),
        sink=sink,
        knowledge_gateway=knowledge,
        checkpointer=checkpointer,
    )
    result = await runtime.run(
        execution_id="exec-no-retrieval",
        snapshot=make_snapshot(knowledge_base_ids=["kb-1"], retrieval_enabled=False),
        user=USER,
        user_text="hello",
    )
    assert result.status == "completed"
    assert knowledge.calls == []
    assert "knowledge_retrieved" not in sink.types()


async def test_reused_model_tool_call_id_requires_fresh_approval(checkpointer) -> None:
    sink = FakeSink()
    tools = FakeTools([make_tool("wire_money", "high", requires_approval=True)])
    model = FakeModel(
        [
            {"tool_calls": [{"id": "call_0", "name": "wire_money", "arguments": {"amount": 1}}]},
            {"tool_calls": [{"id": "call_0", "name": "wire_money", "arguments": {"amount": 2}}]},
            {"content": "finished"},
        ]
    )
    runtime = RuntimeService(
        model_gateway=model, tool_gateway=tools, sink=sink, checkpointer=checkpointer
    )
    first = await runtime.run(
        execution_id="exec-reused-id",
        snapshot=make_snapshot(tool_names=["wire_money"]),
        user=USER,
        user_text="pay twice",
    )
    assert first.status == "waiting_approval"
    first_approval = first.approval_ids[0]
    second = await runtime.resume(
        execution_id="exec-reused-id",
        user=USER,
        snapshot=make_snapshot(tool_names=["wire_money"]),
        decision={"approved": True, "comment": "first"},
        interrupt_id=sink.interrupt_ids[first_approval],
    )
    assert second.status == "waiting_approval"
    assert len(sink.approvals) == 2
    second_approval = next(
        approval_id for approval_id in sink.approvals if approval_id != first_approval
    )
    assert sink.approvals[second_approval]["approval_key"] != sink.approvals[first_approval]["approval_key"]
    assert len(tools.invocations) == 1
    done = await runtime.resume(
        execution_id="exec-reused-id",
        user=USER,
        snapshot=make_snapshot(tool_names=["wire_money"]),
        decision={"approved": True, "comment": "second"},
        interrupt_id=sink.interrupt_ids[second_approval],
    )
    assert done.status == "completed"
    assert len(tools.invocations) == 2


async def test_literal_braces_in_prompt_do_not_crash(checkpointer) -> None:
    sink = FakeSink()
    model = FakeModel([{"content": "ok"}])
    runtime = RuntimeService(
        model_gateway=model,
        tool_gateway=FakeTools(),
        sink=sink,
        checkpointer=checkpointer,
    )
    result = await runtime.run(
        execution_id="exec-literal-braces",
        snapshot=make_snapshot(
            system_prompt='Return JSON exactly: {"a.b": 1, "n": 2}'
        ),
        user=USER,
        user_text="format it",
    )
    assert result.status == "completed"
    assert '{"a.b": 1, "n": 2}' in model.calls[0]["messages"][0].content
