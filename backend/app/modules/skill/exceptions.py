from __future__ import annotations

from app.core.exceptions import ValidationError


class SkillDefinitionError(ValidationError):
    """A skill directory or its metadata is malformed."""

    code = "skill_definition_error"


class SkillDependencyError(ValidationError):
    """A skill requirement (tool, permission or sibling skill) is not satisfied."""

    code = "skill_dependency_error"
