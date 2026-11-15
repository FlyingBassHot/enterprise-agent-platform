"""Audit domain pack demo API.

The aggregator mounts this router under ``/api/v1``; paths are defined here
exactly as ``/audit/sample-file`` and ``/audit/rules``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.api.dependencies import require_permission
from app.core.contracts import ToolContext, UserContext
from app.core.exceptions import NotFoundError
from app.domain_plugins.audit.analysis import RULES
from app.domain_plugins.audit.tools.report import generate_audit_report_tool

SAMPLE_FILE = Path(__file__).resolve().parents[3] / "seed" / "audit_sample.xlsx"
SAMPLE_LABELS = Path(__file__).resolve().parents[3] / "seed" / "audit_labels.json"

router = APIRouter(tags=["audit"])


class AuditRuleInfo(BaseModel):
    rule: str
    name: str
    description: str
    severity: str
    default_options: dict[str, Any] = Field(default_factory=dict)


class AuditRulesResponse(BaseModel):
    total: int
    rules: list[AuditRuleInfo]
    sample_file_available: bool
    sample_labels_available: bool


class AuditReportRequest(BaseModel):
    artifact_id: str = Field(min_length=1)
    options: dict[str, Any] = Field(default_factory=dict)
    max_anomalies: int = Field(default=20, ge=1, le=100)


class AuditReportResponse(BaseModel):
    format: str
    markdown: str
    truncated: bool
    summary: dict[str, Any]


@router.get("/audit/sample-file", response_class=FileResponse)
async def download_audit_sample(
    _user: UserContext = Depends(require_permission("audit:read")),
) -> FileResponse:
    """Download the committed demo transaction workbook."""

    if not SAMPLE_FILE.is_file():
        raise NotFoundError("审计示例文件不存在")
    return FileResponse(
        SAMPLE_FILE,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=SAMPLE_FILE.name,
    )


@router.get("/audit/rules", response_model=AuditRulesResponse)
async def list_audit_rules(
    _user: UserContext = Depends(require_permission("audit:read")),
) -> AuditRulesResponse:
    """Describe the deterministic audit rules and their default options."""

    return AuditRulesResponse(
        total=len(RULES),
        rules=[AuditRuleInfo(**rule) for rule in RULES],
        sample_file_available=SAMPLE_FILE.is_file(),
        sample_labels_available=SAMPLE_LABELS.is_file(),
    )


@router.post("/audit/report", response_model=AuditReportResponse)
async def generate_report(
    payload: AuditReportRequest,
    user: UserContext = Depends(require_permission("audit:read")),
) -> AuditReportResponse:
    """Deterministically generate the audit report for an uploaded workbook."""

    result = await asyncio.wait_for(
        generate_audit_report_tool(
            payload.model_dump(exclude_none=True),
            ToolContext(user=user, tool_name="generate_audit_report"),
        ),
        timeout=60,
    )
    return AuditReportResponse(**result)
