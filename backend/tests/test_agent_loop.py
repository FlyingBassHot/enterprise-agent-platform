from __future__ import annotations

from collections.abc import Sequence
from typing import Any

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
