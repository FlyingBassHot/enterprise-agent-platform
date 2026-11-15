from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class LoginRequest(BaseModel):
    username: str
    password: str


class BootstrapRequest(BaseModel):
    username: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=8, max_length=128)
    display_name: str = ""
    email: str | None = None
    token: str | None = None


class RoleRef(BaseModel):
    id: str
    name: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    username: str
    email: str | None = None
    display_name: str = ""
    is_active: bool
    is_superuser: bool
    department_id: str | None = None
    roles: list[str] = Field(default_factory=list)
    created_at: datetime


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=8, max_length=128)
    display_name: str = ""
    email: str | None = None
    role_names: list[str] = Field(default_factory=lambda: ["viewer"])
    department_id: str | None = None
    is_superuser: bool = False


class UserUpdate(BaseModel):
    display_name: str | None = None
    email: str | None = None
    password: str | None = Field(default=None, min_length=8, max_length=128)
    is_active: bool | None = None
    department_id: str | None = None
    role_names: list[str] | None = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut


class RoleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str
    is_system: bool
    permissions: list[str] = Field(default_factory=list)
    created_at: datetime


class RoleCreate(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    description: str = ""
    permission_codes: list[str] = Field(default_factory=list)


class RoleUpdate(BaseModel):
    description: str | None = None
    permission_codes: list[str] | None = None


class PermissionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    code: str
    name: str
    description: str
    resource_type: str


class ResourcePermissionGrant(BaseModel):
    resource_type: str
    resource_id: str
    subject_type: str = Field(pattern="^(user|role)$")
    subject_id: str
    actions: list[str] = Field(default_factory=lambda: ["read"])
