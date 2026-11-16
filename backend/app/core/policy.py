"""Policy decisions shared by the runtime and the tool/service layer.

One place answers "may this happen, and does it need a human first". Both the
loop and the service layer call these pure functions, so an approval prompt, a
denial event and an API response cannot drift apart.

Scope note: maker-checker rules (who may approve someone else's request) stay in
the approval service — that is a review decision, not an execution policy.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from app.core.contracts import ToolDescriptor

PLATFORM_APPROVAL_RISK_LEVELS = frozenset({"high", "critical"})


class PolicyEffect(StrEnum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


class PolicyDecision(BaseModel):
    effect: PolicyEffect
    code: str = ""
    reason: str = ""
    obligations: dict[str, Any] = Field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.effect is PolicyEffect.ALLOW


def allow(reason: str = "") -> PolicyDecision:
    return PolicyDecision(effect=PolicyEffect.ALLOW, code="allowed", reason=reason)


def deny(code: str, reason: str) -> PolicyDecision:
    return PolicyDecision(effect=PolicyEffect.DENY, code=code, reason=reason)


def require_approval(code: str, reason: str, **obligations: Any) -> PolicyDecision:
    return PolicyDecision(
        effect=PolicyEffect.REQUIRE_APPROVAL,
        code=code,
        reason=reason,
        obligations=dict(obligations),
    )


def decide_tool_access(
    *,
    enabled: bool,
    required_scopes: Sequence[str],
    has_all_scopes: bool,
    granted: bool,
) -> PolicyDecision:
    if not enabled:
        return deny("tool_disabled", "工具已停用")
    if not required_scopes or has_all_scopes or granted:
        return allow()
    return deny(
        "permission_denied",
        "缺少工具权限: " + ", ".join(sorted(required_scopes)),
    )


def decide_tool_approval(
    descriptor: ToolDescriptor,
    *,
    agent_approval_levels: Sequence[str] = (),
    platform_levels: Sequence[str] = tuple(PLATFORM_APPROVAL_RISK_LEVELS),
) -> PolicyDecision:
    if descriptor.requires_approval:
        return require_approval("approval_required_tool", "工具声明需要人工审批")
    if descriptor.risk_level in set(platform_levels):
        return require_approval(
            "approval_required_risk",
            f"平台策略要求 {descriptor.risk_level} 风险工具必须审批",
            risk_level=descriptor.risk_level,
        )
    if descriptor.risk_level in set(agent_approval_levels):
        return require_approval(
            "approval_required_agent_policy",
            f"Agent 策略要求 {descriptor.risk_level} 风险工具审批",
            risk_level=descriptor.risk_level,
        )
    return allow()


def decide_tool_call(
    descriptor: ToolDescriptor | None,
    *,
    bound: bool,
    access_allowed: bool = True,
    agent_approval_levels: Sequence[str] = (),
) -> PolicyDecision:
    """Compose availability, access and approval into one verdict."""

    if descriptor is None:
        return deny("tool_unavailable", "工具不存在或未注册")
    if not descriptor.enabled:
        return deny("tool_disabled", "工具已停用")
    if not bound:
        return deny("tool_not_bound", "工具未绑定到当前 Agent")
    if not access_allowed:
        return deny("permission_denied", "当前用户无权执行该工具")
    return decide_tool_approval(descriptor, agent_approval_levels=agent_approval_levels)


__all__ = [
    "PLATFORM_APPROVAL_RISK_LEVELS",
    "PolicyDecision",
    "PolicyEffect",
    "allow",
    "decide_tool_access",
    "decide_tool_approval",
    "decide_tool_call",
    "deny",
    "require_approval",
]
