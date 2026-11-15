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
    permissions: list[str] = Field(default_factory=list)
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


class AccessPolicyCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    description: str = ""
    effect: str = Field(default="allow", pattern="^(allow|deny)$")
    resource_type: str = Field(min_length=1, max_length=64)
    action: str = Field(default="*", max_length=32)
    subject_roles: list[str] = Field(default_factory=list)
    subject_departments: list[str] = Field(default_factory=list)
    tenant_ids: list[str] = Field(default_factory=list)
    resource_ids: list[str] = Field(default_factory=list)
    priority: int = Field(default=0, ge=-1000, le=1000)
    enabled: bool = True


class AccessPolicyUpdate(BaseModel):
    description: str | None = None
    effect: str | None = Field(default=None, pattern="^(allow|deny)$")
    resource_type: str | None = None
    action: str | None = None
    subject_roles: list[str] | None = None
    subject_departments: list[str] | None = None
    tenant_ids: list[str] | None = None
    resource_ids: list[str] | None = None
    priority: int | None = None
    enabled: bool | None = None


class AccessPolicyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str
    effect: str
    resource_type: str
    action: str
    subject_roles: list[str] = Field(default_factory=list)
    subject_departments: list[str] = Field(default_factory=list)
    tenant_ids: list[str] = Field(default_factory=list)
    resource_ids: list[str] = Field(default_factory=list)
    priority: int
    enabled: bool
    created_at: datetime
