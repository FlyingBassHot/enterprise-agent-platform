from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.contracts import SkillContext, SkillGateway, UserContext
from app.core.exceptions import (
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from app.modules.permission.service import PermissionService
from app.modules.skill.exceptions import SkillDependencyError
from app.modules.skill.loader import SkillDefinition, scan_skills
from app.modules.skill.models import Skill, SkillDependency, SkillVersion
from app.modules.skill.repository import SkillRepository
from app.modules.skill.schemas import (
    SkillDependencyOut,
    SkillDetailOut,
    SkillOut,
    SkillVersionOut,
)


def _canonical(value: Any) -> str:
    return json.dumps(value or {}, sort_keys=True, separators=(",", ":"), default=str)


def _declared_version(latest: SkillVersion | None, fallback: int = 1) -> str:
    if latest is not None and latest.snapshot:
        declared = latest.snapshot.get("version")
        if isinstance(declared, str | int | float) and str(declared).strip():
            return str(declared)
    return str(fallback)


class SkillService(SkillGateway):
    """Filesystem-backed skill registry, version store and runtime resolver.

    Skill instructions are untrusted business guidance. This service only
    decides *which* instructions a user may see; it never lets skill content
    change permissions, tools or platform policy.
    """

    def __init__(self, db: AsyncSession, scan_root: Path | None = None) -> None:
        self.db = db
        self.repo = SkillRepository(db)
        self.scan_root = scan_root

    # -------------------------------------------------------------------- sync

    async def sync(self, actor: UserContext) -> list[SkillOut]:
        """Register skills found on disk, versioning only real changes."""

        definitions = scan_skills(self.scan_root)
        existing = {skill.name: skill for skill in await self.repo.list_skills()}
        synced: dict[str, Skill] = {}

        for definition in definitions:
            skill = existing.get(definition.name)
            if skill is None:
                skill = Skill(
                    name=definition.name,
                    description=definition.description,
                    current_version=1,
                    enabled=False,
                    source_path=definition.source_path,
                    required_tools=list(definition.required_tools),
                    required_permissions=list(definition.required_permissions),
                    inputs=list(definition.inputs),
                    outputs=list(definition.outputs),
                    metadata_json=definition.metadata,
                )
                self.db.add(skill)
                await self.db.flush()
                self.repo.add_version(self._version_row(skill, definition, 1))
            else:
                latest = await self.repo.latest_version(skill.id)
                self._apply_definition(skill, definition)
                snapshot = definition.snapshot()
                if latest is None or not self._unchanged(latest, definition, snapshot):
                    next_version = (latest.version if latest else 0) + 1
                    skill.current_version = next_version
                    self.repo.add_version(
                        self._version_row(skill, definition, next_version, snapshot=snapshot)
                    )
                else:
                    skill.current_version = latest.version
            await self._sync_dependencies(skill, definition)
            synced[skill.name] = skill

        await self.db.commit()
        latest_versions = await self.repo.latest_versions()
        return [
            self._out(synced[name], latest_versions.get(synced[name].id))
            for name in sorted(synced)
        ]

    # ----------------------------------------------------------------- queries

    async def list_skills(self, actor: UserContext) -> list[SkillOut]:
        skills = await self.repo.list_skills()
        latest_versions = await self.repo.latest_versions()
        return [self._out(skill, latest_versions.get(skill.id)) for skill in skills]

    async def get_skill(self, skill_id_or_name: str) -> SkillDetailOut:
        skill = await self._load_or_404(skill_id_or_name)
        latest = await self.repo.latest_version(skill.id)
        dependencies = await self.repo.list_dependencies(skill.id)
        return self._detail_out(skill, latest, dependencies)

    async def list_versions(self, skill_id_or_name: str) -> list[SkillVersionOut]:
        skill = await self._load_or_404(skill_id_or_name)
        return [
            SkillVersionOut.model_validate(row)
            for row in await self.repo.list_versions(skill.id)
        ]

    # ------------------------------------------------------------------ enable

    async def set_enabled(
        self, skill_id: str, enabled: bool, actor: UserContext
    ) -> SkillOut:
        skill = await self._load_or_404(skill_id)
        if enabled:
            await self._ensure_tools_available(skill, actor)
        skill.enabled = enabled
        await self.db.commit()
        latest = await self.repo.latest_version(skill.id)
        return self._out(skill, latest)

    # ---------------------------------------------------------------- resolve

    async def resolve_for_agent(
        self, skill_names: Sequence[str], user: UserContext
    ) -> list[SkillContext]:
        """Return enabled skills for an execution; any gap fails closed."""

        requested = self._requested_names(skill_names)
        if not requested:
            return []
        skills = await self.repo.get_many_by_names(requested)
        self._ensure_available(requested, skills)
        latest_versions = await self.repo.versions_for([skills[name].id for name in requested])

        missing_permissions: set[str] = set()
        for name in requested:
            for code in skills[name].required_permissions or []:
                if not user.has(code):
                    missing_permissions.add(code)
        if missing_permissions:
            raise PermissionDeniedError(
                "Missing required skill permissions: " + ", ".join(sorted(missing_permissions)),
                details={"missing_permissions": sorted(missing_permissions)},
            )

        return [
            self._context(skills[name], latest_versions.get(skills[name].id))
            for name in requested
        ]

    async def validate_bindings(
        self,
        skill_names: Sequence[str],
        actor: UserContext,
        bound_tools: Sequence[str] = (),
    ) -> list[SkillContext]:
        """Validate Agent skill bindings; used at publish time."""

        requested = self._requested_names(skill_names)
        if not requested:
            return []
        skills = await self.repo.get_many_by_names(requested)
        self._ensure_available(requested, skills)
        latest_versions = await self.repo.versions_for([skills[name].id for name in requested])

        bound = set(bound_tools or ())
        missing_tools: dict[str, list[str]] = {}
        required_permissions: set[str] = set()
        for name in requested:
            skill = skills[name]
            absent = sorted(set(skill.required_tools or []) - bound)
            if absent:
                missing_tools[name] = absent
            required_permissions.update(skill.required_permissions or [])
        if missing_tools:
            raise SkillDependencyError(
                "Skill required tools must be bound to the Agent: "
                + ", ".join(f"{name} ({', '.join(tools)})" for name, tools in missing_tools.items()),
                details={"missing_tools": missing_tools},
            )
        if required_permissions:
            known = {
                permission.code
                for permission in await PermissionService(self.db).repo.permissions_by_codes(
                    sorted(required_permissions)
                )
            }
            unknown = sorted(required_permissions - known)
            if unknown:
                raise SkillDependencyError(
                    "Unknown skill permissions: " + ", ".join(unknown),
                    details={"unknown_permissions": unknown},
                )

        return [
            self._context(skills[name], latest_versions.get(skills[name].id))
            for name in requested
        ]

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _requested_names(skill_names: Sequence[str]) -> list[str]:
        return [name.strip() for name in dict.fromkeys(skill_names or ()) if name and name.strip()]

    @staticmethod
    def _ensure_available(requested: Sequence[str], skills: dict[str, Skill]) -> None:
        missing = [name for name in requested if name not in skills]
        if missing:
            raise ValidationError(
                "Unknown skills: " + ", ".join(missing),
                details={"missing_skills": missing},
            )
        disabled = [name for name in requested if not skills[name].enabled]
        if disabled:
            raise ValidationError(
                "Disabled skills: " + ", ".join(disabled),
                details={"disabled_skills": disabled},
            )

    async def _ensure_tools_available(self, skill: Skill, actor: UserContext) -> None:
        required = list(skill.required_tools or [])
        if not required:
            return
        # Lazy import keeps the tool execution stack out of the skill import path.
        from app.modules.tool.service import ToolService

        # ``describe`` fails on unknown or disabled tools and filters by the
        # actor's execution permissions. Existence/disabled state is what we
        # need here; permission filtering only narrows the returned list.
        await ToolService(self.db).describe(required, actor)

    async def _sync_dependencies(
        self, skill: Skill, definition: SkillDefinition
    ) -> None:
        desired = {
            ("tool", name) for name in definition.required_tools
        } | {("permission", code) for code in definition.required_permissions}
        existing = await self.repo.list_dependencies(skill.id)
        present = {(row.kind, row.ref): row for row in existing}
        for key, row in present.items():
            if key not in desired:
                await self.db.delete(row)
        for kind, ref in sorted(desired):
            if (kind, ref) not in present:
                self.repo.add_dependency(SkillDependency(skill_id=skill.id, kind=kind, ref=ref))

    @staticmethod
    def _version_row(
        skill: Skill,
        definition: SkillDefinition,
        version: int,
        *,
        snapshot: dict[str, Any] | None = None,
    ) -> SkillVersion:
        return SkillVersion(
            skill_id=skill.id,
            version=version,
            content=definition.instructions,
            snapshot=snapshot if snapshot is not None else definition.snapshot(),
        )

    @staticmethod
    def _unchanged(
        latest: SkillVersion, definition: SkillDefinition, snapshot: dict[str, Any]
    ) -> bool:
        return latest.content == definition.instructions and _canonical(
            latest.snapshot
        ) == _canonical(snapshot)

    @staticmethod
    def _apply_definition(skill: Skill, definition: SkillDefinition) -> None:
        skill.description = definition.description
        skill.source_path = definition.source_path
        skill.required_tools = list(definition.required_tools)
        skill.required_permissions = list(definition.required_permissions)
        skill.inputs = list(definition.inputs)
        skill.outputs = list(definition.outputs)
        skill.metadata_json = definition.metadata

    @staticmethod
    def _out(skill: Skill, latest: SkillVersion | None) -> SkillOut:
        return SkillOut(
            id=skill.id,
            name=skill.name,
            description=skill.description,
            version=_declared_version(latest, skill.current_version or 1),
            current_version=skill.current_version,
            enabled=skill.enabled,
            source_path=skill.source_path,
            required_tools=list(skill.required_tools or []),
            required_permissions=list(skill.required_permissions or []),
            inputs=list(skill.inputs or []),
            outputs=list(skill.outputs or []),
            metadata=dict(skill.metadata_json or {}),
            created_at=skill.created_at,
            updated_at=skill.updated_at,
        )

    @classmethod
    def _detail_out(
        cls,
        skill: Skill,
        latest: SkillVersion | None,
        dependencies: Sequence[SkillDependency],
    ) -> SkillDetailOut:
        base = cls._out(skill, latest)
        return SkillDetailOut(
            **base.model_dump(),
            instructions=latest.content if latest else "",
            dependencies=[
                SkillDependencyOut.model_validate(dependency) for dependency in dependencies
            ],
        )

    @staticmethod
    def _context(skill: Skill, latest: SkillVersion | None) -> SkillContext:
        return SkillContext(
            name=skill.name,
            version=_declared_version(latest, skill.current_version or 1),
            instructions=latest.content if latest else "",
            required_tools=list(skill.required_tools or []),
            required_permissions=list(skill.required_permissions or []),
        )

    async def _load_or_404(self, skill_id_or_name: str) -> Skill:
        skill = await self.repo.get(skill_id_or_name)
        if skill is None:
            skill = await self.repo.get_by_name(skill_id_or_name)
        if skill is None:
            raise NotFoundError(f"Skill {skill_id_or_name} not found")
        return skill
