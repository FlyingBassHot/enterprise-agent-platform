"""Seed the baseline general assistant Agent.

Every deployment needs one Agent that works before any domain pack exists.
This Agent binds the platform tools and nothing else, so it also acts as the
smoke test for the tool registry, the runtime loop and the approval path.

Service instances are passed in by the caller (seed script or tests), so this
module never creates database sessions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.core.contracts import UserContext
from app.core.exceptions import ValidationError
from app.modules.tool.builtin import TOOL_NAMES

AGENT_NAME = "general-assistant"
AGENT_DESCRIPTION = (
    "平台内置通用助手：查询时间、读取上传附件、通过白名单访问外部服务。"
    "用于验证平台基础能力，可作为新建 Agent 的起点。"
)
PROMPT_NAME = "general-assistant"
PROMPT_DESCRIPTION = "平台内置通用助手系统提示词，覆盖工具使用、安全约束与升级边界。"
PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "general_assistant.md"


def load_system_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8").strip()


def _value(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _as_dict(obj: Any) -> dict[str, Any] | None:
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    return None


def agent_config(
    model_config_id: str, prompt_id: str, prompt_version_id: str
) -> dict[str, Any]:
    """Build the Agent config in the same normalized shape the API stores.

    Going through ``AgentConfig`` keeps idempotency checks honest: defaults
    such as ``execution.workflow_id`` are materialized exactly once.
    """

    from app.modules.agent.schemas import AgentConfig

    raw = {
        "model": {
            "config_id": model_config_id,
            "temperature": 0.2,
            "max_tokens": 2048,
            "top_p": 1.0,
        },
        "prompt": {
            "prompt_id": prompt_id,
            "version_id": prompt_version_id,
            "variables": {},
        },
        "tools": list(TOOL_NAMES),
        "knowledge_base_ids": [],
        "skill_names": [],
        "retrieval": {"enabled": False, "top_k": 4},
        "execution": {
            "mode": "agent",
            "max_iterations": 8,
            "timeout_seconds": 300,
            "approval_risk_levels": ["high", "critical"],
            "streaming": True,
        },
        "permissions": {"required_permissions": [], "allowed_roles": []},
        "metadata": {
            "platform": "builtin",
            "role": "general-assistant",
            "toolset": "platform-baseline",
            "toolset_version": "1.0.0",
        },
    }
    return AgentConfig.model_validate(raw).model_dump(mode="json")


async def _ensure_prompt(prompt_service: Any, actor: UserContext) -> tuple[str, str, int]:
    from app.modules.prompt.schemas import PromptCreate, PromptVersionCreate

    system_prompt = load_system_prompt()
    prompts = await prompt_service.list_prompts()
    prompt = next((item for item in prompts if _value(item, "name") == PROMPT_NAME), None)
    if prompt is None:
        prompt = await prompt_service.create_prompt(
            PromptCreate(name=PROMPT_NAME, description=PROMPT_DESCRIPTION)
        )
    prompt_id = _value(prompt, "id")
    versions = await prompt_service.list_versions(prompt_id)
    if versions:
        latest = max(versions, key=lambda item: _value(item, "version", 0))
        if _value(latest, "system_prompt") == system_prompt:
            return prompt_id, _value(latest, "id"), int(_value(latest, "version", 0))
    version = await prompt_service.create_version(
        prompt_id,
        PromptVersionCreate(
            system_prompt=system_prompt,
            task_prompt="",
            tool_instructions="",
            variables=[],
            publish=True,
        ),
        actor.user_id,
    )
    return prompt_id, _value(version, "id"), int(_value(version, "version", 0))


async def seed_general_assistant(
    agent_service: Any,
    prompt_service: Any,
    tool_service: Any,
    actor: UserContext,
    model_config_id: str,
) -> dict[str, Any]:
    """Create or update the built-in general assistant and publish it.

    Idempotent: an already matching published Agent is left untouched, so
    repeated seeding does not create new versions.
    """

    from app.modules.agent.schemas import AgentCreate, AgentUpdate

    descriptors = await tool_service.describe(list(TOOL_NAMES), actor)
    available = {_value(descriptor, "name") for descriptor in descriptors}
    missing = sorted(set(TOOL_NAMES) - available)
    if missing:
        raise ValidationError(f"平台基础工具未注册或不可执行: {', '.join(missing)}")

    prompt_id, prompt_version_id, prompt_version = await _ensure_prompt(prompt_service, actor)
    config = agent_config(model_config_id, prompt_id, prompt_version_id)

    agents = await agent_service.list_agents(actor)
    agent = next((item for item in agents if _value(item, "name") == AGENT_NAME), None)
    if agent is None:
        agent = await agent_service.create_agent(
            AgentCreate(
                name=AGENT_NAME, description=AGENT_DESCRIPTION, config=config
            ),
            actor,
        )
        agent_id = _value(agent, "id")
    else:
        agent_id = _value(agent, "id")
        already_seeded = (
            _value(agent, "status") == "published"
            and _value(agent, "current_version_id") is not None
            and _as_dict(_value(agent, "draft_config")) == config
        )
        if already_seeded:
            return _result(
                agent,
                agent_id=agent_id,
                prompt_id=prompt_id,
                prompt_version=prompt_version,
                prompt_version_id=prompt_version_id,
                model_config_id=model_config_id,
            )
        await agent_service.update_agent(agent_id, AgentUpdate(config=config), actor)

    validation = await agent_service.validate(agent_id, actor)
    if not _value(validation, "valid", False):
        errors = "；".join(_value(validation, "errors", []) or [])
        raise ValidationError(f"通用助手校验失败: {errors or '未知错误'}")

    agent = await agent_service.publish(agent_id, actor)
    return _result(
        agent,
        agent_id=agent_id,
        prompt_id=prompt_id,
        prompt_version=prompt_version,
        prompt_version_id=prompt_version_id,
        model_config_id=model_config_id,
    )


def _result(
    agent: Any,
    *,
    agent_id: str,
    prompt_id: str,
    prompt_version: int,
    prompt_version_id: str,
    model_config_id: str,
) -> dict[str, Any]:
    return {
        "agent_id": _value(agent, "id", agent_id),
        "agent_name": _value(agent, "name", AGENT_NAME),
        "agent_status": _value(agent, "status"),
        "agent_version": _value(agent, "current_version"),
        "agent_version_id": _value(agent, "current_version_id"),
        "prompt_id": prompt_id,
        "prompt_version": prompt_version,
        "prompt_version_id": prompt_version_id,
        "tool_names": list(TOOL_NAMES),
        "model_config_id": model_config_id,
    }


__all__ = [
    "AGENT_NAME",
    "agent_config",
    "load_system_prompt",
    "seed_general_assistant",
]
