"""Seed the audit demo prompt and Agent.

Service instances are passed in by the caller (a seed script or tests), so this
module never creates database sessions or requires network access.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from app.core.contracts import UserContext
from app.core.exceptions import ValidationError
from app.domain_plugins.audit.prompts import (
    AUDIT_PROMPT_DESCRIPTION,
    AUDIT_PROMPT_NAME,
    load_system_prompt,
)
from app.domain_plugins.audit.tools import TOOL_NAMES

AGENT_NAME = "audit-analyst"
AGENT_DESCRIPTION = "财务交易异常审计分析 Agent，使用确定性规则检测重复付款、大额交易和数据质量问题。"
KNOWLEDGE_BASE_NAME = "audit-rules"
KNOWLEDGE_DOCUMENT = "audit_rules.md"
SKILL_NAME = "bank_transaction_audit"
KNOWLEDGE_SOURCE = Path(__file__).resolve().parent / "knowledge" / "audit_rules.md"
WORKFLOW_NAME = "audit-review"
TOOL_INSTRUCTIONS = (
    "优先使用 analyze_transactions 获取确定性结果，再用 generate_audit_report 生成报告。"
)


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


def _agent_config(
    model_config_id: str,
    prompt_id: str,
    prompt_version_id: str,
    *,
    knowledge_base_ids: list[str] | None = None,
    skill_names: list[str] | None = None,
) -> dict[str, Any]:
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
        "knowledge_base_ids": list(knowledge_base_ids or []),
        "skill_names": list(skill_names or []),
        "retrieval": {"enabled": bool(knowledge_base_ids), "top_k": 4},
        "execution": {
            "mode": "agent",
            "max_iterations": 8,
            "timeout_seconds": 300,
            "approval_risk_levels": ["high", "critical"],
            "streaming": True,
        },
        "permissions": {
            "required_permissions": ["audit:read"],
            "allowed_roles": [],
        },
        "metadata": {
            "domain": "audit",
            "plugin": "audit",
            "plugin_version": "1.0.0",
            "sample_file": "seed/audit_sample.xlsx",
            "labels_file": "seed/audit_labels.json",
        },
    }
    # Normalize through the schema so the idempotency check compares the same
    # shape the API stores (defaults such as execution.workflow_id included).
    return AgentConfig.model_validate(raw).model_dump(mode="json")


def _config_matches(stored: Any, expected: dict[str, Any]) -> bool:
    return _as_dict(stored) == expected


def _result(
    agent: Any,
    *,
    agent_id: str,
    prompt_id: str,
    prompt_version: int,
    prompt_version_id: str,
    model_config_id: str,
    knowledge_base_id: str | None = None,
    skill_name: str | None = None,
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
        "knowledge_base_id": knowledge_base_id,
        "skill_name": skill_name,
    }


async def _ensure_prompt(prompt_service: Any, actor: UserContext) -> tuple[str, str, int]:
    from app.modules.prompt.schemas import PromptCreate, PromptVersionCreate

    system_prompt = load_system_prompt()
    prompts = await prompt_service.list_prompts()
    prompt = next((item for item in prompts if _value(item, "name") == AUDIT_PROMPT_NAME), None)

    if prompt is None:
        prompt = await prompt_service.create_prompt(
            PromptCreate(name=AUDIT_PROMPT_NAME, description=AUDIT_PROMPT_DESCRIPTION)
        )

    prompt_id = _value(prompt, "id")
    versions = await prompt_service.list_versions(prompt_id)
    if versions:
        latest = max(versions, key=lambda item: _value(item, "version", 0))
        latest_prompt = _value(latest, "system_prompt")
        latest_instructions = _value(latest, "tool_instructions") or ""
        if latest_prompt == system_prompt and latest_instructions == TOOL_INSTRUCTIONS:
            return prompt_id, _value(latest, "id"), int(_value(latest, "version", 0))

    version = await prompt_service.create_version(
        prompt_id,
        PromptVersionCreate(
            system_prompt=system_prompt,
            task_prompt="",
            tool_instructions=TOOL_INSTRUCTIONS,
            variables=[],
            publish=True,
        ),
        actor.user_id,
    )
    return prompt_id, _value(version, "id"), int(_value(version, "version", 0))


async def _verify_tools(tool_service: Any, actor: UserContext) -> None:
    descriptors = await tool_service.describe(list(TOOL_NAMES), actor)
    available = {_value(descriptor, "name") for descriptor in descriptors}
    missing = sorted(set(TOOL_NAMES) - available)
    if missing:
        raise ValidationError(f"审计工具未注册: {', '.join(missing)}")


async def _ensure_knowledge(knowledge_service: Any, actor: UserContext) -> str | None:
    if knowledge_service is None:
        return None
    from starlette.datastructures import UploadFile

    from app.modules.knowledge.schemas import KnowledgeBaseCreate

    bases = await knowledge_service.list_bases(actor)
    base = next((item for item in bases if _value(item, "name") == KNOWLEDGE_BASE_NAME), None)
    if base is None:
        base = await knowledge_service.create_base(
            actor,
            KnowledgeBaseCreate(
                name=KNOWLEDGE_BASE_NAME,
                description="审计规则与报告规范（审计演示知识库）",
                access_scope="public",
            ),
        )
    base_id = _value(base, "id")
    documents = await knowledge_service.list_documents(actor, base_id)
    document = next(
        (item for item in documents if _value(item, "filename") == KNOWLEDGE_DOCUMENT),
        None,
    )
    if document is None:
        data = KNOWLEDGE_SOURCE.read_bytes()
        upload = UploadFile(
            file=io.BytesIO(data),
            filename=KNOWLEDGE_DOCUMENT,
            size=len(data),
        )
        document = await knowledge_service.upload_document(actor, base_id, upload)
    document_id = _value(document, "id")
    if _value(document, "status") != "ready":
        await knowledge_service.wait_for_document(document_id, timeout=60)
    return base_id


async def _ensure_skill(skill_service: Any, actor: UserContext) -> str | None:
    if skill_service is None:
        return None
    await skill_service.sync(actor)
    skills = await skill_service.list_skills(actor)
    skill = next((item for item in skills if _value(item, "name") == SKILL_NAME), None)
    if skill is None:
        raise ValidationError(f"审计 Skill 未找到: {SKILL_NAME}")
    skill_id = _value(skill, "id")
    if not _value(skill, "enabled"):
        await skill_service.set_enabled(skill_id, True, actor)
    return SKILL_NAME


def _workflow_definition(agent_id: str) -> dict[str, Any]:
    return {
        "version": 1,
        "start_node": "start",
        "output": "",
        "nodes": [
            {"id": "start", "type": "start", "name": "开始", "next": "audit", "config": {}},
            {
                "id": "audit",
                "type": "agent",
                "name": "审计分析",
                "config": {
                    "agent_id": agent_id,
                    "input": "{{input}}",
                    "output_key": "audit_result",
                    "max_iterations": 6,
                },
                "next": "check",
            },
            {
                "id": "check",
                "type": "condition",
                "name": "是否存在异常",
                "config": {"expression": 'audit_result contains "异常"'},
                "on_true": "review",
                "on_false": "end",
            },
            {
                "id": "review",
                "type": "approval",
                "name": "人工复核",
                "config": {
                    "title": "审计结果复核",
                    "description": "请确认异常清单后再生成正式报告",
                    "on_reject": "fail",
                },
                "next": "end",
            },
            {
                "id": "end",
                "type": "end",
                "name": "结束",
                "config": {"output_key": "audit_result"},
            },
        ],
    }


async def _ensure_workflow(
    workflow_service: Any, actor: UserContext, agent_id: str
) -> dict[str, Any] | None:
    if workflow_service is None:
        return None
    from app.modules.workflow.schemas import WorkflowCreate, WorkflowDefinition, WorkflowUpdate

    definition_json = _workflow_definition(agent_id)
    definition = WorkflowDefinition.model_validate(definition_json)
    workflows = await workflow_service.list_workflows(actor)
    workflow = next(
        (item for item in workflows if _value(item, "name") == WORKFLOW_NAME), None
    )
    if workflow is None:
        workflow = await workflow_service.create_workflow(
            actor,
            WorkflowCreate(
                name=WORKFLOW_NAME,
                description="审计 Agent + 条件判断 + 人工复核的演示工作流",
                definition=definition,
            ),
        )
    elif (
        _value(workflow, "status") != "published"
        or _value(workflow, "definition") != definition
    ):
        workflow = await workflow_service.update_workflow(
            actor,
            _value(workflow, "id"),
            WorkflowUpdate(definition=definition),
        )
    if _value(workflow, "status") != "published":
        workflow = await workflow_service.publish(actor, _value(workflow, "id"))
    return {
        "workflow_id": _value(workflow, "id"),
        "workflow_version": _value(workflow, "current_version"),
    }


async def seed_audit_demo(
    agent_service: Any,
    prompt_service: Any,
    tool_service: Any,
    actor: UserContext,
    model_config_id: str,
    *,
    knowledge_service: Any = None,
    skill_service: Any = None,
    workflow_service: Any = None,
) -> dict[str, Any]:
    """Create or update the audit prompt and ``audit-analyst`` Agent.

    ``agent_service``/``prompt_service``/``tool_service`` are service instances
    bound to an ``AsyncSession``. ``actor`` is the seeding ``UserContext``.
    ``model_config_id`` selects the model configuration bound to the Agent.

    Returns prompt/agent ids and the published agent version.
    """

    from app.modules.agent.schemas import AgentCreate, AgentUpdate

    await _verify_tools(tool_service, actor)
    knowledge_base_id = await _ensure_knowledge(knowledge_service, actor)
    skill_name = await _ensure_skill(skill_service, actor)
    prompt_id, prompt_version_id, prompt_version = await _ensure_prompt(prompt_service, actor)
    config = _agent_config(
        model_config_id,
        prompt_id,
        prompt_version_id,
        knowledge_base_ids=[knowledge_base_id] if knowledge_base_id else [],
        skill_names=[skill_name] if skill_name else [],
    )

    agents = await agent_service.list_agents(actor)
    agent = next((item for item in agents if _value(item, "name") == AGENT_NAME), None)

    if agent is None:
        agent = await agent_service.create_agent(
            AgentCreate(name=AGENT_NAME, description=AGENT_DESCRIPTION, config=config),
            actor,
        )
        agent_id = _value(agent, "id")
    else:
        agent_id = _value(agent, "id")
        already_seeded = (
            _value(agent, "status") == "published"
            and _value(agent, "current_version_id") is not None
            and _config_matches(_value(agent, "draft_config"), config)
        )
        if already_seeded:
            workflow_info = await _ensure_workflow(workflow_service, actor, agent_id)
            result = _result(
                agent,
                agent_id=agent_id,
                prompt_id=prompt_id,
                prompt_version=prompt_version,
                prompt_version_id=prompt_version_id,
                model_config_id=model_config_id,
                knowledge_base_id=knowledge_base_id,
                skill_name=skill_name,
            )
            if workflow_info:
                result.update(workflow_info)
            return result
        await agent_service.update_agent(agent_id, AgentUpdate(config=config), actor)

    validation = await agent_service.validate(agent_id, actor)
    if not _value(validation, "valid", False):
        errors = "；".join(_value(validation, "errors", []) or [])
        raise ValidationError(f"审计 Agent 校验失败: {errors or '未知错误'}")

    agent = await agent_service.publish(agent_id, actor)
    workflow_info = await _ensure_workflow(workflow_service, actor, agent_id)
    result = _result(
        agent,
        agent_id=agent_id,
        prompt_id=prompt_id,
        prompt_version=prompt_version,
        prompt_version_id=prompt_version_id,
        model_config_id=model_config_id,
        knowledge_base_id=knowledge_base_id,
        skill_name=skill_name,
    )
    if workflow_info:
        result.update(workflow_info)
    return result


__all__ = ["AGENT_NAME", "seed_audit_demo"]
