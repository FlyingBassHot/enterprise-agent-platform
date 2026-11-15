from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

VARIABLE_NAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


class PromptVariable(BaseModel):
    name: str = Field(pattern=VARIABLE_NAME_PATTERN, max_length=80)
    required: bool = False
    default: str = ""


class PromptCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    description: str = ""


class PromptUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    description: str | None = None


class PromptVersionCreate(BaseModel):
    system_prompt: str = Field(min_length=1)
    task_prompt: str = ""
    tool_instructions: str = ""
    variables: list[PromptVariable] = Field(default_factory=list)
    publish: bool = True

    @field_validator("variables")
    @classmethod
    def _unique_variables(cls, value: list[PromptVariable]) -> list[PromptVariable]:
        names = [item.name for item in value]
        if len(names) != len(set(names)):
            raise ValueError("Duplicate variable names are not allowed")
        return value


class PromptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str
    current_version: int
    status: str
    created_at: datetime
    updated_at: datetime


class PromptVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    prompt_id: str
    version: int
    system_prompt: str
    task_prompt: str
    tool_instructions: str
    variables: list[PromptVariable] = Field(default_factory=list)
    created_by: str | None = None
    created_at: datetime


class PromptResolveRequest(BaseModel):
    version_id: str | None = None
    variables: dict[str, str] = Field(default_factory=dict)


class ResolvedPrompt(BaseModel):
    prompt_id: str
    version_id: str
    version: int
    system_prompt: str
    task_prompt: str
    tool_instructions: str


def extract_placeholders(*templates: str) -> set[str]:
    names: set[str] = set()
    for template in templates:
        names.update(_PLACEHOLDER_RE.findall(template or ""))
    return names


def render_template(template: str, values: dict[str, str]) -> str:
    return _PLACEHOLDER_RE.sub(lambda match: values[match.group(1)], template or "")
