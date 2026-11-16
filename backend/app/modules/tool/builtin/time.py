"""Deterministic clock tool.

Model providers have no reliable notion of the current time, so the platform
supplies one. The handler is pure: no I/O, no ambient state beyond the clock.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.contracts import ToolContext
from app.core.exceptions import ToolError

DEFAULT_TIMEZONE = "UTC"


async def current_time_tool(arguments: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    name = str(arguments.get("tz") or DEFAULT_TIMEZONE).strip()
    try:
        zone = ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ToolError(f"Unknown timezone '{name}'; use an IANA name such as Asia/Shanghai") from exc
    now = datetime.now(zone)
    offset = now.utcoffset()
    return {
        "timezone": name,
        "iso8601": now.isoformat(timespec="seconds"),
        "date": now.date().isoformat(),
        "weekday": now.strftime("%A"),
        "utc_offset": _format_offset(offset.total_seconds() if offset else 0.0),
        "epoch_seconds": int(now.timestamp()),
    }


def _format_offset(seconds: float) -> str:
    total = int(seconds)
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{sign}{total // 3600:02d}:{total % 3600 // 60:02d}"


__all__ = ["current_time_tool"]
