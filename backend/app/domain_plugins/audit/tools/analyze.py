"""``analyze_transactions`` native tool."""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.contracts import ToolContext
from app.core.exceptions import ToolError
from app.domain_plugins.audit.analysis import analyze_transactions
from app.domain_plugins.audit.excel import parse_transactions
from app.domain_plugins.audit.tools._common import (
    load_artifact_bytes,
    parse_options,
    require_artifact_id,
    require_context_user,
)


async def analyze_transactions_tool(
    arguments: dict[str, Any], ctx: ToolContext
) -> dict[str, Any]:
    artifact_id = require_artifact_id(arguments)
    options = parse_options(arguments)
    require_context_user(ctx)

    try:
        data, filename = await load_artifact_bytes(artifact_id, ctx)
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001 - execution module raises domain errors
        raise ToolError(f"读取交易文件失败: {exc}") from exc

    try:
        parsed = await asyncio.to_thread(parse_transactions, data, filename)
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001 - parser raises ValidationError
        raise ToolError(str(exc)) from exc

    result = await asyncio.to_thread(analyze_transactions, parsed["transactions"], **options)
    payload: dict[str, Any] = {
        "artifact_id": artifact_id,
        "filename": parsed["filename"] or filename,
        "header_row": parsed["header_row"],
        "field_mapping": parsed["field_mapping"],
        "quality_issues": parsed["quality_issues"],
        "anomalies": result["anomalies"],
        "summary": result["summary"],
        "options": result["options"],
    }
    if arguments.get("include_transactions") is True:
        payload["transactions"] = result["transactions"]
    return payload


__all__ = ["analyze_transactions_tool"]
