"""Audit system prompt used by the demo agent."""

from __future__ import annotations

from pathlib import Path

AUDIT_PROMPT_NAME = "audit-system"
AUDIT_PROMPT_DESCRIPTION = "财务交易异常审计分析系统提示词"
SYSTEM_PROMPT_PATH = Path(__file__).resolve().parent / "audit_system.md"


def load_system_prompt() -> str:
    return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip()


__all__ = [
    "AUDIT_PROMPT_DESCRIPTION",
    "AUDIT_PROMPT_NAME",
    "SYSTEM_PROMPT_PATH",
    "load_system_prompt",
]
