"""Read an uploaded artifact through the execution module boundary.

Chat uploads become artifacts, but a generic Agent has no way to look at them
without a template tool. This tool inlines text artifacts for the model and
returns metadata only for binary ones, leaving parsing to domain tools.
"""

from __future__ import annotations

from typing import Any

from app.core.contracts import ToolContext

TEXT_CONTENT_PREFIXES = ("text/",)
TEXT_CONTENT_TYPES = {
    "application/csv",
    "application/json",
    "application/x-ndjson",
    "application/x-yaml",
    "application/xml",
    "application/yaml",
}
TEXT_SUFFIXES = {
    ".csv",
    ".html",
    ".ini",
    ".log",
    ".md",
    ".toml",
    ".tsv",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
DEFAULT_MAX_CHARS = 8000


async def read_artifact_tool(arguments: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from app.modules.execution.artifacts import read_artifact_for_tool

    artifact_id = str(arguments["artifact_id"]).strip()
    info, data = await read_artifact_for_tool(artifact_id, ctx.user)
    result: dict[str, Any] = {
        "artifact_id": info.id,
        "filename": info.filename,
        "content_type": info.content_type or "",
        "size": info.size,
        "content": None,
        "truncated": False,
        "total_chars": 0,
        "note": "",
    }
    if not _is_text(info.filename, info.content_type):
        result["note"] = (
            "二进制附件不直接返回内容；请使用对应的领域工具解析（例如审计 Excel 分析工具）。"
        )
        return result
    text = data.decode("utf-8", errors="replace")
    limit = int(arguments.get("max_chars") or DEFAULT_MAX_CHARS)
    result.update(
        content=text[:limit],
        truncated=len(text) > limit,
        total_chars=len(text),
    )
    return result


def _is_text(filename: str, content_type: str | None) -> bool:
    normalized = (content_type or "").split(";", 1)[0].strip().lower()
    if normalized in TEXT_CONTENT_TYPES or normalized.startswith(TEXT_CONTENT_PREFIXES):
        return True
    lowered = filename.lower()
    return any(lowered.endswith(suffix) for suffix in TEXT_SUFFIXES)


__all__ = ["read_artifact_tool"]
