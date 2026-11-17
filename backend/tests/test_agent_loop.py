from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import pytest

from app.core.config import get_settings
from app.core.contracts import ToolExecutionResult, ToolInvocation, UserContext
from app.core.plan import PLAN_TOOL_NAME, normalize_plan
from app.modules.runtime.service import RuntimeService
from tests.test_runtime import (
    USER,
    FakeModel,
    FakeSink,
    FakeTools,
    checkpointer,  # noqa: F401 - pytest fixture
    make_snapshot,
    make_tool,
)


class CountingTools(FakeTools):
    def __init__(self, descriptors: Sequence[Any] = ()) -> None:
        super().__init__(descriptors)
        self.describe_calls = 0

    async def describe(self, names: Sequence[str], user: UserContext) -> list[Any]:
        self.describe_calls += 1
        return await super().describe(names, user)


class PlanTools(FakeTools):
    async def execute(
        self, invocation: ToolInvocation, user: UserContext
    ) -> ToolExecutionResult:
        self.invocations.append(invocation)
        if invocation.tool_name == PLAN_TOOL_NAME:
            plan = normalize_plan(invocation.arguments.get("plan"))
            return ToolExecutionResult(
                ok=True,
                status="success",
                output={"plan": plan, "count": len(plan or [])},
                duration_ms=1,
            )
        return await super().execute(invocation, user)


def _runtime(model: Any, tools: Any, sink: FakeSink, checkpointer: Any) -> RuntimeService:  # noqa: F811
    return RuntimeService(
        model_gateway=model,
        tool_gateway=tools,
        sink=sink,
        checkpointer=checkpointer,
    )


async def test_tool_descriptors_are_resolved_once_per_run(checkpointer) -> None:  # noqa: F811
    sink = FakeSink()
    model = FakeModel(
        [
            {"tool_calls": [{"name": "echo", "arguments": {"value": 1}}]},
            {"content": "done"},
        ]
    )
    tools = CountingTools([make_tool("echo", "low")])
    runtime = _runtime(model, tools, sink, checkpointer)

    result = await runtime.run(
        execution_id="exec-describe-once",
        snapshot=make_snapshot(tool_names=["echo"]),
        user=USER,
        user_text="go",
    )

    assert result.status == "completed"
    assert result.output_text == "done"
    assert tools.describe_calls == 1
    assert model.calls[0]["tools"] == ["echo"]


async def test_iteration_budget_returns_partial_answer(checkpointer) -> None:  # noqa: F811
    sink = FakeSink()
    model = FakeModel(
        [
            {
                "content": "already learned something",
                "tool_calls": [{"name": "echo", "arguments": {"value": 1}}],
            },
            {"content": "unreachable"},
        ]
    )
    tools = FakeTools([make_tool("echo", "low")])
    runtime = _runtime(model, tools, sink, checkpointer)

    result = await runtime.run(
        execution_id="exec-budget-partial",
        snapshot=make_snapshot(tool_names=["echo"], max_iterations=1),
        user=USER,
        user_text="go",
    )

    assert result.status == "incomplete"
    assert result.output_text == "already learned something"
    assert result.output_payload["partial"] is True
    assert result.output_payload["stop_reason"] == "max_iterations"
    assert len(tools.invocations) == 1
    assert result.error is None


async def test_iteration_budget_without_text_fails(checkpointer) -> None:  # noqa: F811
    sink = FakeSink()
    model = FakeModel([{"tool_calls": [{"name": "echo", "arguments": {"value": 1}}]}])
    tools = FakeTools([make_tool("echo", "low")])
    runtime = _runtime(model, tools, sink, checkpointer)

    result = await runtime.run(
        execution_id="exec-budget-silent",
        snapshot=make_snapshot(tool_names=["echo"], max_iterations=1),
        user=USER,
        user_text="go",
    )

    assert result.status == "failed"
    assert result.error is not None
    assert "maximum iterations" in result.error


async def test_resume_extends_the_iteration_budget(checkpointer) -> None:  # noqa: F811
    sink = FakeSink()
    model = FakeModel(
        [
            {
                "content": "partial",
                "tool_calls": [{"name": "echo", "arguments": {"value": 1}}],
            },
            {"content": "finished after resume"},
        ]
    )
    tools = FakeTools([make_tool("echo", "low")])
    snapshot = make_snapshot(tool_names=["echo"], max_iterations=1)
    runtime = _runtime(model, tools, sink, checkpointer)

    first = await runtime.run(
        execution_id="exec-resume-budget",
        snapshot=snapshot,
        user=USER,
        user_text="go",
    )
    assert first.status == "incomplete"

    second = await runtime.resume(
        execution_id="exec-resume-budget",
        user=USER,
        snapshot=snapshot,
        extra_iterations=2,
    )
    assert second.status == "completed"
    assert second.output_text == "finished after resume"
    assert second.output_payload.get("partial") is not True


async def test_plan_survives_tool_rounds_and_is_reinjected(checkpointer) -> None:  # noqa: F811
    sink = FakeSink()
    plan = [
        {"id": "1", "title": "读取附件", "status": "in_progress"},
        {"id": "2", "title": "汇总并回答", "status": "pending"},
    ]
    model = FakeModel(
        [
            {"tool_calls": [{"name": PLAN_TOOL_NAME, "arguments": {"plan": plan}}]},
            {"content": "answer"},
        ]
    )
    tools = PlanTools([make_tool(PLAN_TOOL_NAME, "low")])
    runtime = _runtime(model, tools, sink, checkpointer)

    result = await runtime.run(
        execution_id="exec-plan",
        snapshot=make_snapshot(tool_names=[PLAN_TOOL_NAME]),
        user=USER,
        user_text="do two things",
    )

    assert result.status == "completed"
    assert result.output_payload["plan"] == plan
    assert "plan_updated" in sink.types()

    second_turn = model.calls[1]["messages"]
    plan_messages = [
        message
        for message in second_turn
        if message.role == "system" and "Current Plan" in message.content
    ]
    assert plan_messages, "plan must be re-injected on the next turn"
    assert "读取附件" in plan_messages[0].content
    # The injected plan is derived state, never persisted into the transcript.
    stored = [message.content for message in second_turn if message.role == "system"]
    assert any("Current Plan" in content for content in stored)


async def test_compact_messages_summarizes_old_tool_results() -> None:
    from app.modules.runtime.context import compact_messages

    big = json.dumps(
        {"status": "success", "ok": True, "output": {"payload": "x" * 5000}},
        ensure_ascii=False,
    )
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "echo"}]},
        {"role": "tool", "tool_call_id": "c1", "name": "echo", "content": big},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c2", "name": "echo"}]},
        {"role": "tool", "tool_call_id": "c2", "name": "echo", "content": big},
    ]

    compacted, saved = compact_messages(messages, budget_chars=2000, keep_recent=2)

    assert saved > 0
    summary = json.loads(compacted[3]["content"])
    assert summary["compacted"] is True
    assert summary["original_chars"] == len(big)
    assert summary["output_keys"] == ["payload"]
    assert compacted[5]["content"] == big
    assert compacted[0]["content"] == "sys"
    assert compacted[1]["content"] == "go"
    # The persisted transcript is never mutated.
    assert messages[3]["content"] == big


async def test_compact_messages_is_a_noop_under_budget() -> None:
    from app.modules.runtime.context import compact_messages

    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "go"},
    ]
    compacted, saved = compact_messages(messages, budget_chars=100_000, keep_recent=1)
    assert saved == 0
    assert compacted == messages


class BigOutputTools(FakeTools):
    async def execute(
        self, invocation: ToolInvocation, user: UserContext
    ) -> ToolExecutionResult:
        self.invocations.append(invocation)
        return ToolExecutionResult(
            ok=True,
            status="success",
            output={"payload": "x" * 3000},
            duration_ms=1,
        )


class TimeoutTools(FakeTools):
    async def execute(
        self, invocation: ToolInvocation, user: UserContext
    ) -> ToolExecutionResult:
        self.invocations.append(invocation)
        return ToolExecutionResult(
            ok=False, status="timeout", error="upstream slow", duration_ms=5
        )


async def test_long_transcript_is_compacted_for_the_model(
    monkeypatch: pytest.MonkeyPatch,
    checkpointer,  # noqa: F811 - pytest fixture
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "context_char_budget", 4000)
    monkeypatch.setattr(settings, "context_keep_recent_messages", 2)

    sink = FakeSink()
    model = FakeModel(
        [
            {"tool_calls": [{"name": "echo", "arguments": {"n": 1}}]},
            {"tool_calls": [{"name": "echo", "arguments": {"n": 2}}]},
            {"tool_calls": [{"name": "echo", "arguments": {"n": 3}}]},
            {"content": "done"},
        ]
    )
    tools = BigOutputTools([make_tool("echo", "low")])
    runtime = _runtime(model, tools, sink, checkpointer)

    result = await runtime.run(
        execution_id="exec-compaction",
        snapshot=make_snapshot(tool_names=["echo"], max_iterations=6),
        user=USER,
        user_text="go",
    )

    assert result.status == "completed"
    assert "context_compacted" in sink.types()

    tool_messages = [message for message in model.calls[3]["messages"] if message.role == "tool"]
    assert len(tool_messages) == 3
    first = json.loads(tool_messages[0].content)
    second = json.loads(tool_messages[1].content)
    assert first["compacted"] is True
    assert second["compacted"] is True
    assert "payload" in json.loads(tool_messages[2].content)["output"]


async def test_tool_timeout_reports_actionable_error(checkpointer) -> None:  # noqa: F811
    sink = FakeSink()
    model = FakeModel(
        [
            {"tool_calls": [{"name": "echo", "arguments": {}}]},
            {"content": "ok"},
        ]
    )
    tools = TimeoutTools([make_tool("echo", "low")])
    runtime = _runtime(model, tools, sink, checkpointer)

    result = await runtime.run(
        execution_id="exec-timeout-guidance",
        snapshot=make_snapshot(tool_names=["echo"]),
        user=USER,
        user_text="go",
    )

    assert result.status == "completed"
    tool_message = next(
        message for message in model.calls[1]["messages"] if message.role == "tool"
    )
    payload = json.loads(tool_message.content)
    assert payload["error_code"] == "tool_timeout"
    assert payload["retryable"] is True
    assert payload["hint"]
    assert payload["error"] == "upstream slow"


async def test_unbound_tool_call_reports_unavailable(checkpointer) -> None:  # noqa: F811
    sink = FakeSink()
    model = FakeModel(
        [
            {"tool_calls": [{"name": "ghost", "arguments": {}}]},
            {"content": "cannot do that"},
        ]
    )
    runtime = _runtime(model, FakeTools(), sink, checkpointer)

    result = await runtime.run(
        execution_id="exec-unbound-tool",
        snapshot=make_snapshot(tool_names=[]),
        user=USER,
        user_text="go",
    )

    assert result.status == "completed"
    tool_message = next(
        message for message in model.calls[1]["messages"] if message.role == "tool"
    )
    payload = json.loads(tool_message.content)
    assert payload["error_code"] == "tool_unavailable"
    assert payload["retryable"] is False


class FakeTaskGateway:
    def __init__(self, steps: Sequence[dict[str, str]] = ()) -> None:
        self.steps = [dict(step) for step in steps]
        self.applied: list[list[dict[str, str]]] = []

    async def steps_for_execution(self, execution_id: str) -> list[dict[str, str]]:
        return [dict(step) for step in self.steps]

    async def apply_plan(
        self, execution_id: str, plan: Sequence[dict[str, str]]
    ) -> None:
        self.applied.append([dict(step) for step in plan])
        self.steps = [dict(step) for step in plan]


async def test_task_plan_is_loaded_and_reinjected(checkpointer) -> None:  # noqa: F811
    steps = [{"id": "1", "title": "读取附件", "status": "in_progress"}]
    gateway = FakeTaskGateway(steps)
    sink = FakeSink()
    model = FakeModel([{"content": "done"}])
    runtime = RuntimeService(
        model_gateway=model,
        tool_gateway=FakeTools(),
        sink=sink,
        checkpointer=checkpointer,
        task_gateway=gateway,
    )

    result = await runtime.run(
        execution_id="exec-task-plan",
        snapshot=make_snapshot(),
        user=USER,
        user_text="go",
    )

    assert result.status == "completed"
    assert result.output_payload["plan"] == steps
    first_turn = model.calls[0]["messages"]
    assert any(
        message.role == "system" and "读取附件" in message.content
        for message in first_turn
    )


async def test_plan_updates_are_persisted_to_the_task(checkpointer) -> None:  # noqa: F811
    plan = [
        {"id": "1", "title": "读取附件", "status": "in_progress"},
        {"id": "2", "title": "汇总", "status": "pending"},
    ]
    gateway = FakeTaskGateway()
    sink = FakeSink()
    model = FakeModel(
        [
            {"tool_calls": [{"name": PLAN_TOOL_NAME, "arguments": {"plan": plan}}]},
            {"content": "done"},
        ]
    )
    runtime = RuntimeService(
        model_gateway=model,
        tool_gateway=PlanTools([make_tool(PLAN_TOOL_NAME, "low")]),
        sink=sink,
        checkpointer=checkpointer,
        task_gateway=gateway,
    )

    result = await runtime.run(
        execution_id="exec-task-plan-write",
        snapshot=make_snapshot(tool_names=[PLAN_TOOL_NAME]),
        user=USER,
        user_text="go",
    )

    assert result.status == "completed"
    assert gateway.applied == [plan]
    assert "plan_updated" in sink.types()


async def test_token_budget_stops_before_executing_tools(checkpointer) -> None:  # noqa: F811
    sink = FakeSink()
    tools = FakeTools([make_tool("echo", "low")])
    model = FakeModel(
        [
            {
                "content": "partial answer",
                "tool_calls": [{"name": "echo", "arguments": {"value": 1}}],
            }
        ]
    )
    runtime = RuntimeService(
        model_gateway=model,
        tool_gateway=tools,
        sink=sink,
        checkpointer=checkpointer,
    )

    result = await runtime.run(
        execution_id="exec-token-guard",
        snapshot=make_snapshot(tool_names=["echo"], token_budget=10),
        user=USER,
        user_text="go",
    )

    assert result.status == "incomplete"
    assert result.output_text == "partial answer"
    assert tools.invocations == []
    assert result.output_payload["stop_reason"] == "token_budget"
    assert result.output_payload["token_budget"] == 10
    assert "budget_exhausted" in sink.types()


async def test_token_budget_without_text_fails(checkpointer) -> None:  # noqa: F811
    sink = FakeSink()
    tools = FakeTools([make_tool("echo", "low")])
    model = FakeModel([{"tool_calls": [{"name": "echo", "arguments": {}}]}])
    runtime = RuntimeService(
        model_gateway=model,
        tool_gateway=tools,
        sink=sink,
        checkpointer=checkpointer,
    )

    result = await runtime.run(
        execution_id="exec-token-guard-silent",
        snapshot=make_snapshot(tool_names=["echo"], token_budget=10),
        user=USER,
        user_text="go",
    )

    assert result.status == "failed"
    assert result.error is not None and "token_budget" in result.error
    assert result.output_payload["stop_reason"] == "token_budget"


async def test_resume_with_extra_tokens_continues(checkpointer) -> None:  # noqa: F811
    sink = FakeSink()
    tools = FakeTools([make_tool("echo", "low")])
    model = FakeModel(
        [
            {"content": "partial", "tool_calls": [{"name": "echo", "arguments": {}}]},
            {"content": "finished after top-up"},
        ]
    )
    snapshot = make_snapshot(tool_names=["echo"], token_budget=10)
    runtime = RuntimeService(
        model_gateway=model,
        tool_gateway=tools,
        sink=sink,
        checkpointer=checkpointer,
    )

    first = await runtime.run(
        execution_id="exec-token-resume",
        snapshot=snapshot,
        user=USER,
        user_text="go",
    )
    assert first.status == "incomplete"

    second = await runtime.resume(
        execution_id="exec-token-resume",
        user=USER,
        snapshot=snapshot,
        extra_tokens=100,
    )
    assert second.status == "completed"
    assert second.output_text == "finished after top-up"
