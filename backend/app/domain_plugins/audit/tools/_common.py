"""Native tool helpers shared by the audit domain pack tools."""

from __future__ import annotations

from typing import Any

from app.core.contracts import ToolContext, UserContext
from app.core.exceptions import ToolError


async def load_artifact_bytes(artifact_id: str, user: UserContext) -> tuple[bytes, str]:
    """Load uploaded artifact bytes through the execution module boundary.

    ``read_artifact_for_tool`` returns ``(ArtifactInfo, bytes)``. The import is
    lazy so the domain pack can be imported while the execution module is
    unavailable (unit tests, documentation generation).
    """

    from app.modules.execution.artifacts import read_artifact_for_tool

    info, data = await read_artifact_for_tool(artifact_id, user)
    if not isinstance(data, (bytes, bytearray)):
        raise ToolError("无法读取上传文件内容")
    filename = getattr(info, "filename", "") or ""
    return bytes(data), filename


def require_artifact_id(arguments: dict[str, Any]) -> str:
    artifact_id = arguments.get("artifact_id")
    if not isinstance(artifact_id, str) or not artifact_id.strip():
        raise ToolError("artifact_id 必须是非空字符串")
    return artifact_id.strip()


def require_context_user(ctx: ToolContext) -> UserContext:
    if ctx is None or not getattr(ctx, "user", None):
        raise ToolError("缺少工具调用上下文")
    return ctx.user


def parse_options(arguments: dict[str, Any]) -> dict[str, Any]:
    raw = arguments.get("options")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ToolError("options 必须是对象")
    unknown = set(raw) - {"large_threshold", "window_days"}
    if unknown:
        raise ToolError(f"不支持的选项: {', '.join(sorted(unknown))}")

    options: dict[str, Any] = {}
    if "large_threshold" in raw:
        threshold = raw["large_threshold"]
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
            raise ToolError("large_threshold 必须是数字")
        options["large_threshold"] = float(threshold)
    if "window_days" in raw:
        window = raw["window_days"]
        if isinstance(window, bool) or not isinstance(window, int):
            raise ToolError("window_days 必须是整数")
        options["window_days"] = window
    return options
