"""Contract tests for the postgraduate extension Skills.

Run from the repository root:
    backend/.venv/bin/python -m pytest extensions/postgraduate/tests/test_skills.py -q
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from app.core.contracts import UserContext
from app.modules.permission.models import Permission
from app.modules.skill.exceptions import SkillDependencyError
from app.modules.skill.loader import scan_skills
from app.modules.skill.models import Skill, SkillDependency, SkillVersion
from app.modules.skill.service import SkillService
from app.modules.tool.models import Tool
from sqlalchemy import delete, select

POSTGRAD_ROOT = Path(__file__).resolve().parents[1] / "agents"
POSTGRAD_AGENTS = {
    "postgrad-university-advisor": {
        "university-research",
        "admission-extraction",
        "source-verification",
        "geo-analysis",
        "admission-risk-analysis",
        "school-recommendation",
    },
    "postgrad-exam-coach": {
        "exam-syllabus-research",
        "past-paper-verification",
        "document-knowledge-extraction",
        "knowledge-graph-builder",
        "chapter-rag-retrieval",
        "practice-question-generator",
        "answer-quality-review",
        "study-delivery",
    },
}
POSTGRAD_ADVISOR_TOOLS = {
    "mcp__postgrad-university-data__search_programs",
    "mcp__postgrad-university-data__get_admission_history",
    "mcp__postgrad-university-data__get_source_evidence",
    "mcp__postgrad-admission-ml__list_models",
    "mcp__postgrad-admission-ml__get_model_info",
    "mcp__postgrad-admission-ml__get_validation_metrics",
    "mcp__postgrad-admission-ml__predict_admission",
    "mcp__postgrad-admission-ml__explain_prediction",
    "mcp__postgrad-admission-ml__compare_models",
}
POSTGRAD_COACH_TOOLS = {
    "mcp__postgrad-exam-knowledge__get_syllabus",
    "mcp__postgrad-exam-knowledge__search_knowledge",
    "mcp__postgrad-exam-knowledge__propose_node",
    "mcp__postgrad-exam-knowledge__propose_relation",
    "mcp__postgrad-exam-knowledge__get_prerequisites",
    "mcp__postgrad-exam-knowledge__find_questions",
    "mcp__postgrad-exam-knowledge__save_verified_question",
    "mcp__postgrad-exam-knowledge__record_attempt",
    "mcp__postgrad-notification__send_email",
    "mcp__postgrad-notification__send_wecom_group",
    "mcp__postgrad-notification__get_delivery_status",
}
POSTGRAD_AGENT_TOOLS = {
    "postgrad-university-advisor": POSTGRAD_ADVISOR_TOOLS,
    "postgrad-exam-coach": POSTGRAD_COACH_TOOLS,
}
POSTGRAD_OPTIONAL_TOOLS = {
    "mcp__postgrad-research__search_web",
    "mcp__postgrad-research__fetch_public_page",
    "mcp__postgrad-research__geocode_campus",
    "mcp__postgrad-documents__parse_document",
    "mcp__postgrad-documents__export_docx",
}
POSTGRAD_ADMIN_ONLY_TOOLS = {
    "mcp__postgrad-university-data__upsert_verified_record",
    "mcp__postgrad-admission-ml__train_model",
    "mcp__postgrad-exam-knowledge__review_proposals",
}
POSTGRAD_REQUIRED_SECTIONS = (
    "触发条件",
    "输入 Schema",
    "允许调用的 MCP",
    "执行步骤",
    "失败与回退",
    "输出 Schema",
    "来源与禁止事项",
    "示例",
    "测试情景",
)
POSTGRAD_TOOL_PATTERN = re.compile(r"mcp__[A-Za-z0-9-]+__[A-Za-z0-9_]+")

ADMIN = UserContext(
    user_id="postgrad-skill-test-user",
    username="postgrad-skill-tester",
    display_name="Postgrad Skill Tester",
    is_superuser=True,
    roles=["admin"],
    permissions=set(),
)


def _postgrad_skills() -> dict:
    return {definition.name: definition for definition in scan_skills(POSTGRAD_ROOT)}


async def _ensure_permission(db, code: str) -> bool:
    if await db.scalar(select(Permission).where(Permission.code == code)) is not None:
        return False
    db.add(Permission(code=code, name=code, resource_type="skill"))
    await db.commit()
    return True


async def _ensure_tool(db, name: str) -> bool:
    if await db.scalar(select(Tool).where(Tool.name == name)) is not None:
        return False
    db.add(Tool(name=name, display_name=name, description="Postgrad test tool"))
    await db.commit()
    return True


def test_postgrad_skills_parse_through_the_real_loader() -> None:
    skills = _postgrad_skills()
    expected = set().union(*POSTGRAD_AGENTS.values())

    assert set(skills) == expected
    for agent, names in POSTGRAD_AGENTS.items():
        for name in names:
            definition = skills[name]
            assert definition.version == "1.0.0"
            assert len(definition.description) >= 20
            assert definition.inputs
            assert definition.outputs
            assert definition.required_permissions == ["tools:execute"]
            assert definition.metadata["extension"] == "postgraduate"
            assert definition.metadata["agent"] == agent
            expected_dir = agent.removeprefix("postgrad-")
            assert definition.source_path.startswith(f"{expected_dir}/skills/{name}/")


def test_postgrad_skills_declare_real_tools_and_complete_sections() -> None:
    known_tools = (
        POSTGRAD_ADVISOR_TOOLS
        | POSTGRAD_COACH_TOOLS
        | POSTGRAD_OPTIONAL_TOOLS
        | POSTGRAD_ADMIN_ONLY_TOOLS
    )
    skills = _postgrad_skills()
    for agent, names in POSTGRAD_AGENTS.items():
        for name in names:
            definition = skills[name]
            body = definition.instructions

            for section in POSTGRAD_REQUIRED_SECTIONS:
                assert f"## {section}" in body, f"{name} missing section {section}"

            # Any MCP name mentioned in the workflow must be a real, declared
            # interface. This catches invented tool names in prose.
            referenced = set(POSTGRAD_TOOL_PATTERN.findall(body))
            assert referenced <= known_tools, f"{name} references {referenced - known_tools}"

            required = set(definition.required_tools)
            assert required
            assert required <= POSTGRAD_AGENT_TOOLS[agent]
            assert required.isdisjoint(POSTGRAD_OPTIONAL_TOOLS)
            assert required.isdisjoint(POSTGRAD_ADMIN_ONLY_TOOLS)

            optional = set(definition.metadata.get("optional_tools", []))
            assert optional <= POSTGRAD_OPTIONAL_TOOLS
            assert optional.isdisjoint(required)


def test_postgrad_system_prompts_define_scope_and_sources() -> None:
    known_tools = (
        POSTGRAD_ADVISOR_TOOLS
        | POSTGRAD_COACH_TOOLS
        | POSTGRAD_OPTIONAL_TOOLS
        | POSTGRAD_ADMIN_ONLY_TOOLS
    )
    for agent in POSTGRAD_AGENTS:
        prompt_file = POSTGRAD_ROOT / agent.removeprefix("postgrad-") / "system.md"
        prompt = prompt_file.read_text(encoding="utf-8")

        assert "职责" in prompt
        assert "追问" in prompt
        assert "来源" in prompt
        assert "输出格式" in prompt
        referenced = set(POSTGRAD_TOOL_PATTERN.findall(prompt))
        assert referenced <= known_tools, f"{prompt_file} references {referenced - known_tools}"


def test_postgrad_agent_tool_surfaces_stay_isolated() -> None:
    skills = _postgrad_skills()

    advisor_required = {
        tool
        for name in POSTGRAD_AGENTS["postgrad-university-advisor"]
        for tool in skills[name].required_tools
    }
    coach_required = {
        tool
        for name in POSTGRAD_AGENTS["postgrad-exam-coach"]
        for tool in skills[name].required_tools
    }

    assert advisor_required <= POSTGRAD_ADVISOR_TOOLS
    assert coach_required <= POSTGRAD_COACH_TOOLS
    assert advisor_required.isdisjoint(coach_required)
    assert not (advisor_required | coach_required) & POSTGRAD_ADMIN_ONLY_TOOLS
    assert not (advisor_required | coach_required) & POSTGRAD_OPTIONAL_TOOLS


async def test_postgrad_skill_dependencies_bind_to_declared_tools(db) -> None:
    created_permission = await _ensure_permission(db, "tools:execute")
    skills = _postgrad_skills()
    tool_names = sorted(
        {tool for definition in skills.values() for tool in definition.required_tools}
    )
    created_tools = [
        name for name in tool_names if await _ensure_tool(db, name)
    ]
    owned_skill_ids: list[str] = []
    try:
        service = SkillService(db, scan_root=POSTGRAD_ROOT)
        synced = await service.sync(ADMIN)
        owned_skill_ids = [skill.id for skill in synced]
        assert {skill.name for skill in synced} == set(skills)

        by_name = {skill.name: skill for skill in synced}
        for names in POSTGRAD_AGENTS.values():
            for name in names:
                await service.set_enabled(by_name[name].id, True, ADMIN)

            bound_tools = sorted(
                {tool for name in names for tool in skills[name].required_tools}
            )
            contexts = await service.validate_bindings(
                names, ADMIN, bound_tools=bound_tools
            )
            assert len(contexts) == len(names)

        # The contract must actually fail when an Agent omits a required tool.
        with pytest.raises(SkillDependencyError):
            await service.validate_bindings(
                ["university-research"], ADMIN, bound_tools=[]
            )
    finally:
        await db.rollback()
        if owned_skill_ids:
            await db.execute(
                delete(SkillDependency).where(
                    SkillDependency.skill_id.in_(owned_skill_ids)
                )
            )
            await db.execute(
                delete(SkillVersion).where(
                    SkillVersion.skill_id.in_(owned_skill_ids)
                )
            )
            await db.execute(delete(Skill).where(Skill.id.in_(owned_skill_ids)))
        if created_tools:
            await db.execute(delete(Tool).where(Tool.name.in_(created_tools)))
        if created_permission:
            await db.execute(
                delete(Permission).where(Permission.code == "tools:execute")
            )
        await db.commit()
