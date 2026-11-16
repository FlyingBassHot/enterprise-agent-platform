from __future__ import annotations

from typing import Any

from app.core.contracts import (
    AgentSnapshot,
    VerificationIssue,
    VerificationOutcome,
)
from app.modules.execution.models import AgentExecution
from app.modules.execution.repository import ExecutionRepository
from app.modules.execution.tasks import TaskService
from app.modules.execution.verification import TaskVerifier
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


def _snapshot(**overrides: Any) -> AgentSnapshot:
    return make_snapshot(**overrides)


class ScriptedVerifier:
    def __init__(self, outcomes: list[VerificationOutcome]) -> None:
        self.outcomes = outcomes
        self.calls: list[dict[str, Any]] = []

    async def verify(
        self,
        *,
        execution_id: str,
        snapshot: AgentSnapshot,
        plan: Any,
        output_text: str,
        citations: Any,
    ) -> VerificationOutcome:
        self.calls.append(
            {
                "execution_id": execution_id,
                "plan": list(plan),
                "output_text": output_text,
                "citations": list(citations),
            }
        )
        index = min(len(self.calls) - 1, len(self.outcomes) - 1)
        return self.outcomes[index]


def _failed(code: str = "plan_incomplete") -> VerificationOutcome:
    return VerificationOutcome(
        status="failed",
        issues=[VerificationIssue(code=code, message=f"{code} 未通过")],
        next_action="retry",
    )


def _passed() -> VerificationOutcome:
    return VerificationOutcome(status="passed", issues=[], next_action="complete")


async def _runtime(model: Any, verifier: Any, sink: FakeSink, checkpointer: Any) -> RuntimeService:  # noqa: F811
    return RuntimeService(
        model_gateway=model,
        tool_gateway=FakeTools([make_tool("echo", "low")]),
        sink=sink,
        checkpointer=checkpointer,
        verifier_gateway=verifier,
    )


async def test_empty_answer_fails_verification(db) -> None:
    verifier = TaskVerifier(db)
    outcome = await verifier.verify(
        execution_id="missing-execution",
        snapshot=_snapshot(),
        plan=[],
        output_text="   ",
        citations=[],
    )
    assert outcome.status == "failed"
    assert [issue.code for issue in outcome.issues] == ["empty_answer"]


async def test_unfinished_plan_fails_verification(client, db) -> None:  # noqa: ARG001
    repo = ExecutionRepository(db)
    task = await TaskService(db).ensure_task(
        agent_id="agent-1", user_id="u-1", conversation_id=None, goal="g"
    )
    await TaskService(db).apply_plan_to_task(
        task.id,
        [
            {"id": "1", "title": "读取附件", "status": "done"},
            {"id": "2", "title": "汇总", "status": "in_progress"},
        ],
    )
    execution = await repo.create_execution(
        AgentExecution(
            id="11111111-2222-3333-4444-555555555555",
            task_id=task.id,
            agent_id="agent-1",
            user_id="u-1",
            status="running",
            input_text="go",
        )
    )
    await db.commit()

    outcome = await TaskVerifier(db).verify(
        execution_id=execution.id,
        snapshot=_snapshot(),
        plan=[
            {"id": "1", "title": "读取附件", "status": "done"},
            {"id": "2", "title": "汇总", "status": "in_progress"},
        ],
        output_text="已经完成",
        citations=[],
    )
    assert outcome.status == "failed"
    assert [issue.code for issue in outcome.issues] == ["plan_incomplete"]
    assert outcome.issues[0].step_ids == ["2"]


async def test_passing_verification_marks_done_steps_verified(client, db) -> None:  # noqa: ARG001
    repo = ExecutionRepository(db)
    task = await TaskService(db).ensure_task(
        agent_id="agent-1", user_id="u-1", conversation_id=None, goal="g"
    )
    await TaskService(db).apply_plan_to_task(
        task.id, [{"id": "1", "title": "唯一一步", "status": "done"}]
    )
    execution = await repo.create_execution(
        AgentExecution(
            id="66666666-7777-8888-9999-000000000000",
            task_id=task.id,
            agent_id="agent-1",
            user_id="u-1",
            status="running",
            input_text="go",
        )
    )
    await db.commit()

    outcome = await TaskVerifier(db).verify(
        execution_id=execution.id,
        snapshot=_snapshot(),
        plan=[{"id": "1", "title": "唯一一步", "status": "done"}],
        output_text="完成了",
        citations=[],
    )
    assert outcome.status == "passed"
    steps = await TaskService(db).steps_for_task(task.id)
    assert steps[0].verified is True


async def test_verification_bounce_injects_feedback(checkpointer) -> None:  # noqa: F811
    sink = FakeSink()
    model = FakeModel([{"content": "我完成了"}])
    verifier = ScriptedVerifier([_failed(), _passed()])
    runtime = await _runtime(model, verifier, sink, checkpointer)

    result = await runtime.run(
        execution_id="exec-verify-retry",
        snapshot=_snapshot(verification_max_retries=1),
        user=USER,
        user_text="go",
    )

    assert result.status == "completed"
    assert result.output_text == "我完成了"
    assert len(verifier.calls) == 2
    assert len(model.calls) == 2
    second_turn = model.calls[1]["messages"]
    assert any(
        message.role == "system" and "[系统校验]" in message.content
        for message in second_turn
    )
    assert sink.types().count("verification_started") == 2
    assert sink.types().count("verification_completed") == 2
    assert result.output_payload["verification"]["status"] == "passed"


async def test_verification_exhaustion_returns_incomplete(checkpointer) -> None:  # noqa: F811
    sink = FakeSink()
    model = FakeModel([{"content": "我完成了"}])
    verifier = ScriptedVerifier([_failed(), _failed()])
    runtime = await _runtime(model, verifier, sink, checkpointer)

    result = await runtime.run(
        execution_id="exec-verify-exhausted",
        snapshot=_snapshot(verification_max_retries=1),
        user=USER,
        user_text="go",
    )

    assert result.status == "incomplete"
    assert result.output_text == "我完成了"
    assert result.output_payload["stop_reason"] == "verification_failed"
    assert result.output_payload["verification"]["status"] == "failed"
    assert result.output_payload["verification"]["attempts"] == 2
    assert result.output_payload["verification"]["issues"][0]["code"] == "plan_incomplete"


async def test_verification_can_be_disabled(checkpointer) -> None:  # noqa: F811
    class ExplodingVerifier:
        async def verify(self, **_: Any) -> VerificationOutcome:
            raise AssertionError("verifier must not run when disabled")

    sink = FakeSink()
    model = FakeModel([{"content": "done"}])
    runtime = await _runtime(model, ExplodingVerifier(), sink, checkpointer)

    result = await runtime.run(
        execution_id="exec-verify-off",
        snapshot=_snapshot(verification_enabled=False),
        user=USER,
        user_text="go",
    )

    assert result.status == "completed"
    assert result.output_payload["verification"]["status"] == "skipped"
    assert "verification_started" not in sink.types()
