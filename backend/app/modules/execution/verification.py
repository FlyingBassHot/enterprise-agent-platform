"""Deterministic completion checks.

The verifier answers one question: does this completion claim hold up against
machine-checkable evidence? It never asks a model to judge its own work, so the
same inputs always produce the same verdict.

Level 1/2 only (execution and result checks). Target-level judgement stays with
humans: a failed verdict ends the run as `incomplete`, not `completed`.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.contracts import AgentSnapshot, VerificationIssue, VerificationOutcome
from app.modules.execution.repository import ExecutionRepository
from app.modules.execution.tasks import TaskService

CITATION_PATTERN = re.compile(r"\[?K(\d+)\]?")


class TaskVerifier:
    """Task-aware verifier used by the execution runtime."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = ExecutionRepository(db)

    async def verify(
        self,
        *,
        execution_id: str,
        snapshot: AgentSnapshot,
        plan: Sequence[dict[str, str]],
        output_text: str,
        citations: Sequence[dict[str, Any]],
    ) -> VerificationOutcome:
        issues: list[VerificationIssue] = []
        text = (output_text or "").strip()

        if not text:
            issues.append(
                VerificationIssue(
                    code="empty_answer",
                    message="最终答案为空，不能视为完成。",
                )
            )

        if snapshot.verification_require_plan_completion and plan:
            unfinished = [step for step in plan if step.get("status") != "done"]
            if unfinished:
                issues.append(
                    VerificationIssue(
                        code="plan_incomplete",
                        message="计划仍有未完成步骤："
                        + "；".join(
                            f"{step.get('title') or step.get('id')} ({step.get('status')})"
                            for step in unfinished
                        ),
                        step_ids=[str(step.get("id")) for step in unfinished],
                    )
                )

        if snapshot.verification_require_citations and citations:
            referenced = {match.group(1) for match in CITATION_PATTERN.finditer(text)}
            if not referenced:
                issues.append(
                    VerificationIssue(
                        code="missing_citations",
                        message="本轮检索到知识但答案没有引用任何 [K#] 来源。",
                    )
                )

        if issues:
            return VerificationOutcome(
                status="failed", issues=issues, next_action="retry"
            )

        await self._mark_steps_verified(execution_id)
        return VerificationOutcome(status="passed", issues=[], next_action="complete")

    async def _mark_steps_verified(self, execution_id: str) -> None:
        execution = await self.repo.get_execution(execution_id)
        if execution is None or not execution.task_id:
            return
        steps = await TaskService(self.db).steps_for_task(execution.task_id)
        changed = False
        for step in steps:
            if step.status == "done" and step.verified is not True:
                step.verified = True
                changed = True
        if changed:
            await self.db.commit()


__all__ = ["CITATION_PATTERN", "TaskVerifier"]
