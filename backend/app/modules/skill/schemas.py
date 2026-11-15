from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SkillDependencyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    kind: str
    ref: str


class SkillOut(BaseModel):
    id: str
    name: str
    description: str
    version: str
    current_version: int
    enabled: bool
    source_path: str
    required_tools: list[str] = Field(default_factory=list)
    required_permissions: list[str] = Field(default_factory=list)
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class SkillDetailOut(SkillOut):
    instructions: str = ""
    dependencies: list[SkillDependencyOut] = Field(default_factory=list)


class SkillVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    skill_id: str
    version: int
    content: str
    snapshot: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
