from __future__ import annotations

import pytest

from app.core.contracts import (
    ToolDescriptor,
    ToolInvocation,
    UserContext,
)
from app.core.policy import (
    PolicyEffect,
    decide_tool_access,
    decide_tool_approval,
    decide_tool_call,
    effective_token_budget,
)
from app.modules.permission.service import PermissionService
from app.modules.runtime.service import RuntimeService
from app.modules.tool.models import Tool
from app.modules.tool.service import ToolService
from tests.test_runtime import (
    USER,
    FakeModel,
    FakeSink,
    FakeTools,
    checkpointer,  # noqa: F401 - pytest fixture
    make_snapshot,
    make_tool,
)


def _descriptor(**overrides: object) -> ToolDescriptor:
    base: dict[str, object] = {
        "name": "echo",
        "description": "echo",
        "input_schema": {"type": "object", "properties": {}},
        "risk_level": "low",
        "timeout_seconds": 5,
    }
    base.update(overrides)
    return ToolDescriptor(**base)  # type: ignore[arg-type]


def test_tool_call_decision_matrix() -> None:
    assert decide_tool_call(None, bound=True).code == "tool_unavailable"
    assert decide_tool_call(_descriptor(enabled=False), bound=True).code == "tool_disabled"
    assert decide_tool_call(_descriptor(), bound=False).code == "tool_not_bound"
    denied = decide_tool_call(_descriptor(), bound=True, access_allowed=False)
    assert denied.effect is PolicyEffect.DENY
    assert denied.code == "permission_denied"

    flagged = decide_tool_call(_descriptor(requires_approval=True), bound=True)
    assert flagged.effect is PolicyEffect.REQUIRE_APPROVAL
    assert flagged.code == "approval_required_tool"

    risky = decide_tool_call(_descriptor(risk_level="high"), bound=True)
    assert risky.code == "approval_required_risk"

    agent_policy = decide_tool_call(
        _descriptor(risk_level="medium"),
        bound=True,
        agent_approval_levels=["medium"],
    )
    assert agent_policy.code == "approval_required_agent_policy"

    allowed = decide_tool_call(
        _descriptor(risk_level="medium"),
        bound=True,
        agent_approval_levels=["high"],
    )
    assert allowed.effect is PolicyEffect.ALLOW


def test_tool_access_decision_matrix() -> None:
    assert decide_tool_access(
        enabled=False, required_scopes=[], has_all_scopes=True, granted=False
    ).code == "tool_disabled"
    assert decide_tool_access(
        enabled=True, required_scopes=[], has_all_scopes=False, granted=False
    ).effect is PolicyEffect.ALLOW
    assert decide_tool_access(
        enabled=True, required_scopes=["audit:read"], has_all_scopes=True, granted=False
    ).effect is PolicyEffect.ALLOW
    assert decide_tool_access(
        enabled=True,
        required_scopes=["audit:read"],
        has_all_scopes=False,
        granted=True,
    ).effect is PolicyEffect.ALLOW
    assert decide_tool_access(
        enabled=True,
        required_scopes=["audit:read", "audit:write"],
        has_all_scopes=False,
        granted=False,
    ).code == "permission_denied"


def test_agent_policy_cannot_weaken_platform_floor() -> None:
    decision = decide_tool_approval(
        _descriptor(risk_level="critical"), agent_approval_levels=[]
    )
    assert decision.effect is PolicyEffect.REQUIRE_APPROVAL
    assert decision.code == "approval_required_risk"


async def test_tool_service_denies_missing_scope_with_structured_code(
    client, db
) -> None:  # noqa: ARG001
    tool = Tool(
        name="guarded",
        display_name="guarded",
        description="guarded tool",
        version="1.0.0",
        source_type="http",
        input_schema={"type": "object", "properties": {}},
        risk_level="low",
        timeout_seconds=5,
        permission_scope=["audit:read"],
        config={
            "method": "GET",
            "url": "https://example.com",
            "headers": {},
            "query": {},
        },
        enabled=True,
    )
    db.add(tool)
    await db.commit()

    user = UserContext(
        user_id="u-2", username="viewer", permissions={"tools:execute"}
    )
    result = await ToolService(db).execute(
        ToolInvocation(tool_name="guarded", arguments={}, user_id="u-2"), user
    )

    assert result.status == "denied"
    assert result.ok is False
    assert result.error == "permission_denied"


async def test_approval_event_carries_policy_reason(checkpointer) -> None:  # noqa: F811
    sink = FakeSink()
    tools = FakeTools([make_tool("wire", "high")])
    model = FakeModel([{"tool_calls": [{"name": "wire", "arguments": {"amount": 1}}]}])
    runtime = RuntimeService(
        model_gateway=model,
        tool_gateway=tools,
        sink=sink,
        checkpointer=checkpointer,
    )

    result = await runtime.run(
        execution_id="exec-policy-approval",
        snapshot=make_snapshot(tool_names=["wire"]),
        user=USER,
        user_text="pay",
    )

    assert result.status == "waiting_approval"
    approvals = [event for event in sink.events if event["type"] == "approval_required"]
    assert approvals
    policy = approvals[0]["payload"]["policy"]
    assert policy["effect"] == "require_approval"
    assert policy["code"] == "approval_required_risk"


async def test_resource_decision_is_structured(client, admin, db) -> None:  # noqa: ARG001
    permissions = PermissionService(db)
    owner = await permissions.repo.get_user_by_username("admin")
    assert owner is not None
    actor = await permissions.load_context(owner.id)

    allow = await permissions.decide_resource_access(
        actor,
        resource_type="artifact",
        resource_id="a-1",
        action="read",
        owner_id=actor.user_id,
    )
    assert allow.effect is PolicyEffect.ALLOW

    other = UserContext(user_id="someone-else", username="other")
    deny = await permissions.decide_resource_access(
        other,
        resource_type="artifact",
        resource_id="a-1",
        action="read",
        owner_id=actor.user_id,
    )
    assert deny.effect is PolicyEffect.DENY
    assert deny.code == "permission_denied"

    from app.core.exceptions import PermissionDeniedError

    with pytest.raises(PermissionDeniedError, match="No read access"):
        await permissions.require_resource_access(
            other,
            resource_type="artifact",
            resource_id="a-1",
            action="read",
            owner_id=actor.user_id,
        )


def test_effective_token_budget_is_capped_by_the_platform() -> None:
    assert effective_token_budget(1000, 500) == 500
    assert effective_token_budget(1000, 5000) == 1000
    assert effective_token_budget(0, 5000) == 5000
    assert effective_token_budget(0, None) is None
    assert effective_token_budget(1000, None) == 1000


def test_error_guidance_covers_policy_denial_codes() -> None:
    from app.modules.runtime.context import tool_error_details

    for code in ("tool_not_bound", "invalid_call"):
        resolved, hint, retryable = tool_error_details(code)
        assert resolved == code
        assert hint
        assert retryable is False
