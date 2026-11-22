from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

FileRootPermission = Literal["read", "write"]


class UsableAgentOut(BaseModel):
    """An Agent the current user may enter a workspace for."""

    id: str
    name: str
    description: str
    status: str
    current_version: int
    published_at: datetime | None = None


class WorkspaceConversationCreate(BaseModel):
    title: str = ""
    root_id: str | None = None


class FileRootAccessOut(BaseModel):
    id: str
    name: str
    description: str
    permission: FileRootPermission


class FileRootEntryOut(BaseModel):
    name: str
    path: str
    type: Literal["file", "directory"]
    size: int
    modified_at: datetime | None = None


class FileRootCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = ""


class FileRootOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str
    owner_id: str
    created_at: datetime
    updated_at: datetime


class FileRootGrantCreate(BaseModel):
    agent_id: str
    permission: FileRootPermission = "read"


class FileRootGrantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    agent_id: str
    root_id: str
    permission: FileRootPermission
    agent_name: str = ""
    created_at: datetime
    updated_at: datetime


__all__ = [
    "FileRootAccessOut",
    "FileRootCreate",
    "FileRootEntryOut",
    "FileRootGrantCreate",
    "FileRootGrantOut",
    "FileRootOut",
    "FileRootPermission",
    "UsableAgentOut",
    "WorkspaceConversationCreate",
]
