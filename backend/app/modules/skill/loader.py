"""Filesystem skill loader.

Skills are plain directories with a ``SKILL.md`` whose YAML front matter holds
metadata and whose body holds the instructions. A sibling ``skill.yaml`` may
supply metadata that is missing from the front matter (for example when a
skill ships only instructions inline). Everything loaded here is untrusted
business guidance: the loader only parses and validates shape, it never
executes skill content.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.modules.skill.exceptions import SkillDefinitionError

SKILL_FILENAME = "SKILL.md"
METADATA_FILENAME = "skill.yaml"

# Names double as Agent bindings, so they follow the same conservative shape as
# tool names. Path separators are rejected explicitly to rule out traversal.
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,119}$")
VERSION_PATTERN = re.compile(r"^\d+(?:\.\d+){0,2}$")

DEFAULT_SCAN_ROOT = Path(__file__).resolve().parents[2] / "domain_plugins"


@dataclass(frozen=True)
class SkillDefinition:
    """A validated skill as it exists on disk."""

    name: str
    version: str
    description: str
    instructions: str
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    required_tools: list[str] = field(default_factory=list)
    required_permissions: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    source_path: str = ""

    def snapshot(self) -> dict[str, Any]:
        """Stable metadata snapshot used for change detection and versioning."""

        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "inputs": list(self.inputs),
            "outputs": list(self.outputs),
            "required_tools": list(self.required_tools),
            "required_permissions": list(self.required_permissions),
            "metadata": json.loads(json.dumps(self.metadata, sort_keys=True, default=str)),
        }


def default_scan_root() -> Path:
    return DEFAULT_SCAN_ROOT


def split_front_matter(text: str, *, source: str = "") -> tuple[str | None, str]:
    """Split ``---`` delimited YAML front matter from the markdown body."""

    cleaned = text.lstrip("\ufeff")
    lines = cleaned.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return None, cleaned
    for index in range(1, len(lines)):
        if lines[index].strip() in {"---", "..."}:
            return "".join(lines[1:index]), "".join(lines[index + 1 :]).lstrip("\r\n")
    raise SkillDefinitionError(
        f"Unterminated YAML front matter in {source or SKILL_FILENAME}"
    )


def _parse_yaml_block(raw: str, *, source: str) -> dict[str, Any]:
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise SkillDefinitionError(f"Invalid YAML in {source}: {exc}") from exc
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise SkillDefinitionError(f"Metadata in {source} must be a YAML mapping")
    return {str(key): value for key, value in parsed.items()}


def _as_text(value: Any, *, key: str, source: str) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, int | float) and not isinstance(value, bool):
        return str(value)
    raise SkillDefinitionError(f"Field '{key}' in {source} must be a string")


def _as_list(value: Any, *, key: str, source: str) -> list[str]:
    if value is None:
        return []
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, list | tuple):
        raise SkillDefinitionError(
            f"Field '{key}' in {source} must be a list of strings"
        )
    seen: list[str] = []
    for item in items:
        text = _as_text(item, key=key, source=source)
        if not text:
            raise SkillDefinitionError(
                f"Field '{key}' in {source} contains an empty entry"
            )
        if text not in seen:
            seen.append(text)
    return seen


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_json_safe(item) for item in value]
    return str(value)


def _validate_name(name: str, *, source: str) -> str:
    if not name or not NAME_PATTERN.match(name):
        raise SkillDefinitionError(
            f"Invalid skill name {name!r} in {source}; use letters, digits, '_', "
            "'-', '.' or ':' (max 120 chars)"
        )
    if name in {".", ".."} or ".." in name or "/" in name or "\\" in name:
        raise SkillDefinitionError(
            f"Skill name {name!r} in {source} is not allowed (path traversal)"
        )
    return name


def _validate_version(value: Any, *, source: str) -> str:
    if value is None:
        return "1"
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        raise SkillDefinitionError(f"Invalid skill version in {source}")
    version = str(value).strip()
    if not VERSION_PATTERN.match(version):
        raise SkillDefinitionError(
            f"Invalid skill version {version!r} in {source}; expect e.g. 1, 1.0 or 1.0.0"
        )
    return version


def load_skill(skill_file: Path, *, base: Path | None = None) -> SkillDefinition:
    """Load and validate one ``SKILL.md`` (plus optional ``skill.yaml``)."""

    skill_file = Path(skill_file)
    if skill_file.name != SKILL_FILENAME:
        raise SkillDefinitionError(f"Expected a {SKILL_FILENAME} file: {skill_file}")
    try:
        text = skill_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SkillDefinitionError(f"Cannot read skill file {skill_file}: {exc}") from exc

    front_matter, body = split_front_matter(text, source=str(skill_file))
    raw: dict[str, Any] = {}
    if front_matter is not None:
        raw = _parse_yaml_block(front_matter, source=str(skill_file))

    metadata_file = skill_file.with_name(METADATA_FILENAME)
    if metadata_file.exists():
        try:
            fallback_raw = metadata_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise SkillDefinitionError(
                f"Cannot read skill metadata {metadata_file}: {exc}"
            ) from exc
        for key, value in _parse_yaml_block(fallback_raw, source=str(metadata_file)).items():
            if raw.get(key) is None:
                raw[key] = value

    source = str(skill_file)
    name = _as_text(raw.get("name"), key="name", source=source)
    if not name:
        raise SkillDefinitionError(
            f"Skill at {source} is missing the required 'name' field"
        )
    name = _validate_name(name, source=source)
    version = _validate_version(raw.get("version"), source=source)
    metadata = raw.get("metadata")
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        raise SkillDefinitionError(f"Field 'metadata' in {source} must be a mapping")

    relative_source = source
    if base is not None:
        try:
            relative_source = Path(source).resolve().relative_to(Path(base).resolve()).as_posix()
        except ValueError:
            relative_source = source

    return SkillDefinition(
        name=name,
        version=version,
        description=_as_text(raw.get("description"), key="description", source=source),
        instructions=body.strip(),
        inputs=_as_list(raw.get("inputs"), key="inputs", source=source),
        outputs=_as_list(raw.get("outputs"), key="outputs", source=source),
        required_tools=_as_list(
            raw.get("required_tools"), key="required_tools", source=source
        ),
        required_permissions=_as_list(
            raw.get("required_permissions"), key="required_permissions", source=source
        ),
        metadata=_json_safe(metadata),
        source_path=relative_source,
    )


def scan_skills(scan_root: Path | None = None) -> list[SkillDefinition]:
    """Discover ``<root>/**/skills/<name>/SKILL.md`` and validate every skill."""

    root = Path(scan_root) if scan_root is not None else default_scan_root()
    if not root.exists():
        return []
    if not root.is_dir():
        raise SkillDefinitionError(f"Skill scan root is not a directory: {root}")

    candidates = {
        path for path in root.glob("**/skills/*/SKILL.md") if path.is_file()
    }
    # Tests (and deployments) may point the scan root directly at a skills folder.
    candidates.update(path for path in root.glob("*/SKILL.md") if path.is_file())

    definitions: list[SkillDefinition] = []
    seen: dict[str, Path] = {}
    for skill_file in sorted(candidates, key=lambda path: path.as_posix()):
        definition = load_skill(skill_file, base=root)
        if definition.name in seen:
            raise SkillDefinitionError(
                f"Duplicate skill name {definition.name!r}: "
                f"{seen[definition.name]} and {skill_file}"
            )
        seen[definition.name] = skill_file
        definitions.append(definition)
    return definitions
