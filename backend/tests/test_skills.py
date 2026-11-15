from __future__ import annotations

import shutil
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from httpx import AsyncClient
from sqlalchemy import delete, select

from app.core.config import get_settings
from app.core.contracts import UserContext
from app.core.exceptions import PermissionDeniedError, ValidationError
from app.infrastructure.database.session import init_db
from app.main import app
from app.modules.permission.models import Permission
from app.modules.skill.exceptions import SkillDefinitionError
from app.modules.skill.models import Skill, SkillDependency, SkillVersion
from app.modules.skill.router import router as skill_router
from app.modules.skill.service import SkillService
from app.modules.tool.models import Tool

FIXTURES = Path(__file__).parent / "fixtures" / "skills"

API_PREFIX = get_settings().api_prefix
SKILLS_API = f"{API_PREFIX}/skills"

# The shared API router is owned by the integration step. Include this module's
# router when it is not wired yet so these tests always exercise the real app.
if not any(getattr(route, "path", "").startswith(SKILLS_API) for route in app.router.routes):
    app.include_router(skill_router, prefix=API_PREFIX)


TEST_PERMISSION_CODES = ("skill_unit:read", "skill_unit:write")
TEST_TOOL_NAMES = ("risk_scan", "alpha_tool", "beta_tool")


@pytest.fixture(autouse=True)
async def clean_skill_db(db):
    await init_db()
    await _purge(db)
    await db.commit()
    yield
    await _purge(db)
    await db.commit()


async def _purge(db) -> None:
    await db.execute(delete(SkillDependency))
    await db.execute(delete(SkillVersion))
    await db.execute(delete(Skill))
    await db.execute(delete(Permission).where(Permission.code.in_(TEST_PERMISSION_CODES)))
    await db.execute(delete(Tool).where(Tool.name.in_(TEST_TOOL_NAMES)))


def actor(*permissions: str, superuser: bool = False) -> UserContext:
    return UserContext(
        user_id="skill-test-user",
        username="skill-tester",
        display_name="Skill Tester",
        is_superuser=superuser,
        roles=["admin"] if superuser else ["viewer"],
        permissions=set(permissions),
    )


ADMIN = actor(superuser=True)


def install_fixture(root: Path, name: str) -> Path:
    target = root / "skills" / name
    shutil.copytree(FIXTURES / name, target)
    return target


def write_skill(
    root: Path,
    dirname: str,
    *,
    front: dict | None,
    body: str = "Follow the checklist.",
    skill_yaml: dict | None = None,
) -> Path:
    directory = root / "skills" / dirname
    directory.mkdir(parents=True, exist_ok=True)
    text = ""
    if front is not None:
        text = "---\n" + yaml.safe_dump(front, sort_keys=False) + "---\n\n"
    (directory / "SKILL.md").write_text(text + body, encoding="utf-8")
    if skill_yaml is not None:
        (directory / "skill.yaml").write_text(
            yaml.safe_dump(skill_yaml, sort_keys=False), encoding="utf-8"
        )
    return directory


async def add_permission(db, code: str) -> None:
    if await db.scalar(select(Permission).where(Permission.code == code)) is None:
        db.add(Permission(code=code, name=code, resource_type="skill"))
        await db.commit()


async def add_tool(db, name: str) -> Tool:
    tool = Tool(name=name, display_name=name, description="Test tool")
    db.add(tool)
    await db.commit()
    return tool


# ---------------------------------------------------------------------------
# Sync / versioning
# ---------------------------------------------------------------------------


async def test_sync_creates_disabled_skill_and_versions_changes(db, tmp_path: Path) -> None:
    install_fixture(tmp_path, "report_writer")
    service = SkillService(db, scan_root=tmp_path)

    skills = await service.sync(ADMIN)
    assert [skill.name for skill in skills] == ["report_writer"]
    skill = skills[0]
    assert skill.enabled is False
    assert skill.current_version == 1
    assert skill.version == "2.0.0"
    assert skill.description.startswith("Turn audit findings")
    assert skill.inputs == ["outline"]
    assert skill.outputs == ["report"]
    assert skill.required_tools == []
    assert skill.required_permissions == []
    assert skill.metadata == {"team": "reporting"}
    assert skill.source_path.endswith("report_writer/SKILL.md")

    detail = await service.get_skill(skill.id)
    assert "Write a concise Markdown report" in detail.instructions
    assert detail.dependencies == []

    versions = await service.list_versions(skill.id)
    assert [row.version for row in versions] == [1]
    assert "Write a concise Markdown report" in versions[0].content
    assert versions[0].snapshot["version"] == "2.0.0"

    # An unchanged resync must not create a version.
    assert (await service.sync(ADMIN))[0].current_version == 1
    assert len(await service.list_versions(skill.id)) == 1

    # Enabled state survives resyncs, including one that versions a change.
    enabled = await service.set_enabled(skill.id, True, ADMIN)
    assert enabled.enabled is True
    skill_file = tmp_path / "skills" / "report_writer" / "SKILL.md"
    skill_file.write_text(
        skill_file.read_text(encoding="utf-8") + "\nAlways cite the source row.\n",
        encoding="utf-8",
    )
    updated = (await service.sync(ADMIN))[0]
    assert updated.current_version == 2
    assert updated.enabled is True
    versions = await service.list_versions(skill.id)
    assert [row.version for row in versions] == [1, 2]
    assert "Always cite the source row." in versions[1].content

    # A metadata-only change also versions the skill.
    metadata = tmp_path / "skills" / "report_writer" / "skill.yaml"
    payload = yaml.safe_load(metadata.read_text(encoding="utf-8"))
    payload["description"] = "Updated description"
    metadata.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    assert (await service.sync(ADMIN))[0].current_version == 3
    assert (await service.get_skill("report_writer")).description == "Updated description"


async def test_sync_parses_front_matter_and_sibling_fallback(db, tmp_path: Path) -> None:
    install_fixture(tmp_path, "transaction_review")
    # skill.yaml is only a fallback: front matter wins for shared keys.
    (tmp_path / "skills" / "transaction_review" / "skill.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "wrong_name",
                "version": "9.9.9",
                "description": "fallback description",
                "metadata": {"domain": "fallback"},
            }
        ),
        encoding="utf-8",
    )
    service = SkillService(db, scan_root=tmp_path)
    skill = (await service.sync(ADMIN))[0]

    assert skill.name == "transaction_review"
    assert skill.version == "1.0.0"
    assert skill.description.startswith("Review bank transactions")
    assert skill.required_tools == ["risk_scan"]
    assert skill.required_permissions == ["audit:read"]
    assert skill.metadata == {"domain": "audit"}
    assert "Call `risk_scan`" in (await service.get_skill(skill.id)).instructions

    deps = await db.scalars(select(SkillDependency).where(SkillDependency.skill_id == skill.id))
    pairs = {(dep.kind, dep.ref) for dep in deps}
    assert pairs == {("tool", "risk_scan"), ("permission", "audit:read")}


async def test_sync_rejects_invalid_metadata_and_duplicate_names(db, tmp_path: Path) -> None:
    service = SkillService(db, scan_root=tmp_path)

    write_skill(tmp_path, "bad_name", front={"name": "../evil", "version": "1"})
    with pytest.raises(SkillDefinitionError):
        await service.sync(ADMIN)
    shutil.rmtree(tmp_path / "skills" / "bad_name")

    write_skill(tmp_path, "bad_version", front={"name": "bad_version", "version": "latest"})
    with pytest.raises(SkillDefinitionError):
        await service.sync(ADMIN)
    shutil.rmtree(tmp_path / "skills" / "bad_version")

    write_skill(tmp_path, "missing_name", front={"version": "1"})
    with pytest.raises(SkillDefinitionError):
        await service.sync(ADMIN)
    shutil.rmtree(tmp_path / "skills" / "missing_name")

    write_skill(tmp_path, "dup_one", front={"name": "duplicate_skill", "version": "1"})
    write_skill(tmp_path, "dup_two", front={"name": "duplicate_skill", "version": "1"})
    with pytest.raises(SkillDefinitionError):
        await service.sync(ADMIN)

    # A failed scan must not leave partial rows behind.
    assert list(await db.scalars(select(Skill))) == []


async def test_sync_dedupes_and_updates_dependencies(db, tmp_path: Path) -> None:
    write_skill(
        tmp_path,
        "dependency_skill",
        front={
            "name": "dependency_skill",
            "version": "1.0.0",
            "required_tools": ["alpha_tool", "alpha_tool", "beta_tool"],
            "required_permissions": ["skill_unit:read", "skill_unit:read"],
        },
    )
    service = SkillService(db, scan_root=tmp_path)
    skill = (await service.sync(ADMIN))[0]
    deps = list(
        await db.scalars(
            select(SkillDependency)
            .where(SkillDependency.skill_id == skill.id)
            .order_by(SkillDependency.kind, SkillDependency.ref)
        )
    )
    assert [(dep.kind, dep.ref) for dep in deps] == [
        ("permission", "skill_unit:read"),
        ("tool", "alpha_tool"),
        ("tool", "beta_tool"),
    ]

    write_skill(
        tmp_path,
        "dependency_skill",
        front={
            "name": "dependency_skill",
            "version": "1.1.0",
            "required_tools": ["alpha_tool"],
            "required_permissions": ["skill_unit:read", "skill_unit:write"],
        },
    )
    updated = (await service.sync(ADMIN))[0]
    assert updated.current_version == 2
    deps = list(
        await db.scalars(
            select(SkillDependency)
            .where(SkillDependency.skill_id == skill.id)
            .order_by(SkillDependency.kind, SkillDependency.ref)
        )
    )
    assert [(dep.kind, dep.ref) for dep in deps] == [
        ("permission", "skill_unit:read"),
        ("permission", "skill_unit:write"),
        ("tool", "alpha_tool"),
    ]


# ---------------------------------------------------------------------------
# Enable / resolve
# ---------------------------------------------------------------------------


async def test_enable_requires_existing_tools(db, tmp_path: Path) -> None:
    install_fixture(tmp_path, "transaction_review")
    service = SkillService(db, scan_root=tmp_path)
    skill = (await service.sync(ADMIN))[0]

    with pytest.raises(ValidationError) as excinfo:
        await service.set_enabled(skill.id, True, ADMIN)
    assert "risk_scan" in excinfo.value.message
    assert (await service.get_skill(skill.id)).enabled is False

    await add_tool(db, "risk_scan")
    enabled = await service.set_enabled(skill.id, True, ADMIN)
    assert enabled.enabled is True
    # Resync keeps the operator's enabled decision.
    assert (await service.sync(ADMIN))[0].enabled is True

    disabled = await service.set_enabled(skill.id, False, ADMIN)
    assert disabled.enabled is False


async def test_resolve_for_agent_enforces_state_and_permissions(db, tmp_path: Path) -> None:
    await add_permission(db, "skill_unit:read")
    write_skill(
        tmp_path,
        "review_skill",
        front={
            "name": "review_skill",
            "version": "1.2.0",
            "required_permissions": ["skill_unit:read"],
            "required_tools": [],
        },
        body="Check every row and cite the source.",
    )
    service = SkillService(db, scan_root=tmp_path)
    skill = (await service.sync(ADMIN))[0]
    await service.set_enabled(skill.id, True, ADMIN)

    allowed = actor("skill_unit:read")
    contexts = await service.resolve_for_agent(["review_skill", "review_skill"], allowed)
    assert len(contexts) == 1
    context = contexts[0]
    assert context.name == "review_skill"
    assert context.version == "1.2.0"
    assert "Check every row" in context.instructions
    assert context.required_permissions == ["skill_unit:read"]

    with pytest.raises(PermissionDeniedError) as excinfo:
        await service.resolve_for_agent(["review_skill"], actor())
    assert excinfo.value.details == {"missing_permissions": ["skill_unit:read"]}

    # Superusers bypass individual permission codes.
    assert len(await service.resolve_for_agent(["review_skill"], ADMIN)) == 1

    with pytest.raises(ValidationError) as excinfo:
        await service.resolve_for_agent(["ghost_skill"], allowed)
    assert excinfo.value.details == {"missing_skills": ["ghost_skill"]}

    await service.set_enabled(skill.id, False, ADMIN)
    with pytest.raises(ValidationError) as excinfo:
        await service.resolve_for_agent(["review_skill"], allowed)
    assert excinfo.value.details == {"disabled_skills": ["review_skill"]}


async def test_validate_bindings_checks_tools_and_permissions(db, tmp_path: Path) -> None:
    await add_permission(db, "skill_unit:read")
    write_skill(
        tmp_path,
        "bound_skill",
        front={
            "name": "bound_skill",
            "version": "1.0.0",
            "required_tools": ["alpha_tool"],
            "required_permissions": ["skill_unit:read"],
        },
    )
    write_skill(
        tmp_path,
        "orphan_skill",
        front={
            "name": "orphan_skill",
            "version": "1.0.0",
            "required_permissions": ["skill_unit:missing"],
        },
    )
    await add_tool(db, "alpha_tool")
    service = SkillService(db, scan_root=tmp_path)
    await service.sync(ADMIN)
    bound = await db.scalar(select(Skill).where(Skill.name == "bound_skill"))
    orphan = await db.scalar(select(Skill).where(Skill.name == "orphan_skill"))
    assert bound is not None and orphan is not None
    await service.set_enabled(bound.id, True, ADMIN)
    await service.set_enabled(orphan.id, True, ADMIN)

    with pytest.raises(ValidationError) as excinfo:
        await service.validate_bindings(["bound_skill"], ADMIN, bound_tools=[])
    assert excinfo.value.details == {"missing_tools": {"bound_skill": ["alpha_tool"]}}

    contexts = await service.validate_bindings(
        ["bound_skill"], ADMIN, bound_tools=["alpha_tool", "extra_tool"]
    )
    assert [context.name for context in contexts] == ["bound_skill"]

    with pytest.raises(ValidationError) as excinfo:
        await service.validate_bindings(["orphan_skill"], ADMIN)
    assert excinfo.value.details == {"unknown_permissions": ["skill_unit:missing"]}

    with pytest.raises(ValidationError) as excinfo:
        await service.validate_bindings(["ghost_skill"], ADMIN)
    assert excinfo.value.details == {"missing_skills": ["ghost_skill"]}
    assert await service.validate_bindings([], ADMIN) == []


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


async def _login(client: AsyncClient, username: str, password: str) -> dict[str, str]:
    response = await client.post(
        f"{API_PREFIX}/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def test_skill_api_with_admin(client: AsyncClient, admin: dict[str, str]) -> None:
    response = await client.get(SKILLS_API, headers=admin)
    assert response.status_code == 200, response.text
    assert response.json() == []

    response = await client.post(f"{SKILLS_API}/sync", headers=admin)
    assert response.status_code == 200, response.text
    skills = response.json()
    names = {skill["name"] for skill in skills}
    assert "bank_transaction_audit" in names
    audit = next(skill for skill in skills if skill["name"] == "bank_transaction_audit")
    assert audit["enabled"] is False
    assert audit["current_version"] == 1
    assert set(audit["required_tools"]) == {"analyze_transactions", "generate_audit_report"}
    assert audit["required_permissions"] == ["audit:read"]

    response = await client.get(f"{SKILLS_API}/{audit['id']}", headers=admin)
    assert response.status_code == 200, response.text
    detail = response.json()
    assert "银行流水异常审计方法" in detail["instructions"]
    assert {dep["kind"] for dep in detail["dependencies"]} == {"tool", "permission"}

    response = await client.post(f"{SKILLS_API}/{audit['id']}/enable", headers=admin)
    assert response.status_code == 200, response.text
    assert response.json()["enabled"] is True

    response = await client.post(f"{SKILLS_API}/{audit['id']}/disable", headers=admin)
    assert response.status_code == 200, response.text
    assert response.json()["enabled"] is False

    response = await client.get(f"{SKILLS_API}/{audit['id']}/versions", headers=admin)
    assert response.status_code == 200, response.text
    versions = response.json()
    assert versions and versions[0]["version"] == 1
    assert versions[0]["content"]


async def test_skill_api_requires_write_permission(
    client: AsyncClient, admin: dict[str, str]
) -> None:
    username = f"skill-dev-{uuid4().hex[:8]}"
    response = await client.post(
        f"{API_PREFIX}/users",
        headers=admin,
        json={
            "username": username,
            "password": "developer-pass-123",
            "role_names": ["developer"],
        },
    )
    assert response.status_code == 201, response.text
    headers = await _login(client, username, "developer-pass-123")

    # Developers can read skills but cannot sync or toggle them.
    assert (await client.get(SKILLS_API, headers=headers)).status_code == 200
    assert (await client.post(f"{SKILLS_API}/sync", headers=headers)).status_code == 403
    assert (await client.get(SKILLS_API, headers={})).status_code == 401
