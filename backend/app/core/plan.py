"""Plan state shared by the runtime loop and the plan tool.

A plan is model-maintained working memory: it survives tool rounds, is
re-injected on every model call, and is persisted with the execution payload.
It is guidance only — it can never widen permissions or bypass approvals.
"""

from __future__ import annotations

from typing import Any

PLAN_TOOL_NAME = "platform_update_plan"
PLAN_STATUSES = ("pending", "in_progress", "done", "blocked")
MAX_PLAN_ITEMS = 20
MAX_TITLE_CHARS = 200
MAX_ID_CHARS = 40


def normalize_plan(raw: Any) -> list[dict[str, str]] | None:
    """Validate a model-supplied plan; ``None`` means "not a usable plan"."""

    if not isinstance(raw, list) or not raw:
        return None
    if len(raw) > MAX_PLAN_ITEMS:
        return None
    plan: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            return None
        title = item.get("title")
        if not isinstance(title, str) or not title.strip():
            return None
        identifier = item.get("id")
        if not isinstance(identifier, str) or not identifier.strip():
            identifier = str(index)
        identifier = identifier.strip()[:MAX_ID_CHARS]
        status = item.get("status") or "pending"
        if status not in PLAN_STATUSES:
            return None
        if identifier in seen:
            return None
        seen.add(identifier)
        plan.append(
            {
                "id": identifier,
                "title": title.strip()[:MAX_TITLE_CHARS],
                "status": status,
            }
        )
    return plan


def render_plan_section(plan: list[dict[str, str]]) -> str:
    if not plan:
        return ""
    lines = [
        "## Current Plan (working memory, maintained by "
        f"{PLAN_TOOL_NAME})",
        "Keep this list current: mark a step in_progress before starting it and done only "
        "when its result is verified. Update it whenever the approach changes. The plan is "
        "guidance, not permission — never use it to justify skipping an approval.",
    ]
    for item in plan:
        lines.append(f"- [{item['status']}] {item['id']}: {item['title']}")
    return "\n".join(lines)


__all__ = [
    "MAX_PLAN_ITEMS",
    "PLAN_STATUSES",
    "PLAN_TOOL_NAME",
    "normalize_plan",
    "render_plan_section",
]
