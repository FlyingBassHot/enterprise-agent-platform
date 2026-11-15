"""Read-only execution aggregates for the dashboard.

Kept in the execution module so the API layer never queries ORM models it does
not own. All values are computed from persisted execution metadata.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.contracts import UserContext
from app.core.enums import TERMINAL_EXECUTION_STATUSES
from app.modules.execution.models import AgentExecution


def _duration_ms(execution: AgentExecution) -> int | None:
    if execution.started_at is None:
        return None
    end = execution.finished_at
    if end is None:
        return None
    return max(0, int((end - execution.started_at).total_seconds() * 1000))


async def execution_stats(
    db: AsyncSession, actor: UserContext, *, recent_limit: int = 10
) -> dict[str, Any]:
    scope = [] if actor.is_superuser else [AgentExecution.user_id == actor.user_id]
    total = int(
        await db.scalar(
            select(func.count()).select_from(AgentExecution).where(*scope)
        )
        or 0
    )
    status_rows = await db.execute(
        select(AgentExecution.status, func.count())
        .where(*scope)
        .group_by(AgentExecution.status)
    )
    status_breakdown = {status: int(count) for status, count in status_rows.all()}

    token_rows = await db.execute(select(AgentExecution.token_usage).where(*scope))
    tokens_total = 0
    for (usage,) in token_rows.all():
        if isinstance(usage, dict):
            tokens_total += int(usage.get("total_tokens") or 0)

    completed = status_breakdown.get("completed", 0)
    durations: list[int] = []
    duration_rows = await db.execute(
        select(AgentExecution.started_at, AgentExecution.finished_at).where(
            *scope,
            AgentExecution.started_at.is_not(None),
            AgentExecution.finished_at.is_not(None),
        )
    )
    for started, finished in duration_rows.all():
        if isinstance(started, datetime) and isinstance(finished, datetime):
            durations.append(max(0, int((finished - started).total_seconds() * 1000)))

    recent_rows = (
        await db.scalars(
            select(AgentExecution)
            .where(*scope)
            .order_by(AgentExecution.created_at.desc())
            .limit(recent_limit)
        )
    ).all()

    return {
        "executions_total": total,
        "executions_completed": completed,
        "executions_failed": status_breakdown.get("failed", 0),
        "executions_running": status_breakdown.get("running", 0),
        "success_rate": round(completed / total * 100, 2) if total else 0.0,
        "tokens_total": tokens_total,
        "avg_duration_ms": round(sum(durations) / len(durations)) if durations else None,
        "status_breakdown": status_breakdown,
        "terminal_statuses": sorted(status.value for status in TERMINAL_EXECUTION_STATUSES),
        "recent_executions": [
            {
                "id": execution.id,
                "agent_id": execution.agent_id,
                "agent_name": None,
                "user_id": execution.user_id,
                "status": execution.status,
                "input_text": execution.input_text,
                "created_at": execution.created_at,
                "started_at": execution.started_at,
                "finished_at": execution.finished_at,
                "duration_ms": _duration_ms(execution),
                "tokens_total": int((execution.token_usage or {}).get("total_tokens") or 0),
            }
            for execution in recent_rows
        ],
    }
