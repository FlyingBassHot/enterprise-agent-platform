"""Maintain the loop's plan / todo list.

The handler is intentionally dumb: it validates the submitted plan and returns
it. The runtime applies the returned plan to graph state and re-injects it on
every model call, so the plan survives tool rounds instead of drifting inside
the message history.
"""

from __future__ import annotations

from typing import Any

from app.core.contracts import ToolContext
from app.core.exceptions import ToolError
from app.core.plan import MAX_PLAN_ITEMS, normalize_plan


async def update_plan_tool(arguments: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    plan = normalize_plan(arguments.get("plan"))
    if plan is None:
        raise ToolError(
            "Invalid plan: pass 1-"
            f"{MAX_PLAN_ITEMS} items with a non-empty title and status in "
            "pending / in_progress / done / blocked"
        )
    return {"plan": plan, "count": len(plan)}


__all__ = ["update_plan_tool"]
