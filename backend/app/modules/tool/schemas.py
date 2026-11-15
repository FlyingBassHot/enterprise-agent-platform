from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

from app.core.config import get_settings
from app.modules.tool.adapters.http import ENV_REF_PATTERN

RiskLevel = Literal["low", "medium", "high", "critical"]
ToolSource = Literal["native", "http"]
HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
BodyMode = Literal["json", "form"]

NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$"

SENSITIVE_HEADERS = {
    "authorization",
    "proxy-authorization",
    "x-api-key",
    "api-key",
    "apikey",
    "x-auth-token",
    "cookie",
}
_SECRET_LIKE = (
    re.compile(r"^sk-[A-Za-z0-9_\-]{8,}$"),
    re.compile(r"^[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}$"),
    re.compile(r"^[0-9a-fA-F]{32,}$"),
)


def _invalid(message: str) -> PydanticCustomError:
    return PydanticCustomError("invalid_tool_definition", message)


def _validate_object_schema(schema: dict[str, Any], label: str) -> dict[str, Any]:
    if not isinstance(schema, dict):
        raise _invalid(f"{label} must be a JSON object schema")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise _invalid(f"{label} is not a valid JSON Schema: {exc.message}") from exc
    if schema.get("type", "object") != "object":
        raise _invalid(f"{label} must have top-level type 'object'")
    return schema


def _normalize_scope(values: list[str]) -> list[str]:
    seen: list[str] = []
    for value in values:
        code = (value or "").strip()
        if not code:
            raise _invalid("permission_scope entries must be non-empty permission codes")
        if code not in seen:
            seen.append(code)
    return seen


def _normalize_fields(values: list[str]) -> list[str]:
    seen: list[str] = []
    for value in values:
        name = (value or "").strip()
        if not name:
            raise _invalid("sensitive_fields entries must be non-empty field names")
        if name not in seen:
            seen.append(name)
    return seen


class HttpToolConfig(BaseModel):
    """Declarative HTTP tool. Secrets only via ``${ENV_VAR}`` header references."""

    model_config = ConfigDict(extra="forbid")

    method: HttpMethod = "GET"
    url: str = Field(min_length=8, max_length=2000)
    headers: dict[str, str] = Field(default_factory=dict)
    query: dict[str, str] = Field(default_factory=dict)
    body_mode: BodyMode = "json"
    timeout_seconds: int | None = Field(default=None, ge=1, le=600)

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        value = value.strip()
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"}:
            raise _invalid("url must use the http or https scheme")
        if not parts.hostname:
            raise _invalid("url must include a host")
        if parts.username or parts.password:
            raise _invalid("url must not embed credentials; use ${ENV_VAR} header references")
        return value

    @field_validator("query")
    @classmethod
    def _check_query(cls, value: dict[str, str]) -> dict[str, str]:
        cleaned: dict[str, str] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key.strip():
                raise _invalid("query parameter names must be non-empty strings")
            if not isinstance(item, str):
                raise _invalid("query parameter values must be strings")
            cleaned[key] = item
        return cleaned

    @field_validator("headers")
    @classmethod
    def _check_headers(cls, value: dict[str, str]) -> dict[str, str]:
        cleaned: dict[str, str] = {}
        for raw_name, raw_value in value.items():
            name = (raw_name or "").strip()
            if not name:
                raise _invalid("header names must be non-empty strings")
            if not isinstance(raw_value, str):
                raise _invalid("header values must be strings")
            if "\n" in raw_value or "\r" in raw_value:
                raise _invalid(f"header {name} must not contain newlines")
            lowered = name.lower()
            refs = ENV_REF_PATTERN.findall(raw_value)
            prefix = get_settings().tool_secret_env_prefix
            for ref in refs:
                if not ref.startswith(prefix):
                    raise _invalid(
                        f"header {name} references {ref}; environment refs must start with {prefix}"
                    )
            if lowered in SENSITIVE_HEADERS and not ENV_REF_PATTERN.search(raw_value):
                raise _invalid(
                    f"header {name} must reference an environment variable, e.g. "
                    f"${{{name.upper().replace('-', '_')}_TOKEN}}; literal secrets are rejected"
                )
            literal = ENV_REF_PATTERN.sub("", raw_value).strip()
            if any(pattern.match(literal) for pattern in _SECRET_LIKE):
                raise _invalid(
                    f"header {name} looks like a literal secret; use a ${{ENV_VAR}} reference"
                )
            cleaned[name] = raw_value
        return cleaned


class ToolCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120, pattern=NAME_PATTERN)
    display_name: str = Field(default="", max_length=160)
    description: str = ""
    source_type: ToolSource = "native"
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    risk_level: RiskLevel | None = None
    timeout_seconds: int | None = Field(default=None, ge=1, le=600)
    permission_scope: list[str] | None = None
    requires_approval: bool | None = None
    enabled: bool = True
    sensitive_fields: list[str] = Field(default_factory=list)
    config: HttpToolConfig | None = None

    @field_validator("permission_scope")
    @classmethod
    def _scope(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _normalize_scope(value)

    @field_validator("sensitive_fields")
    @classmethod
    def _sensitive(cls, value: list[str]) -> list[str]:
        return _normalize_fields(value)

    @field_validator("input_schema", "output_schema")
    @classmethod
    def _schema(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        return _validate_object_schema(value, "schema")

    @model_validator(mode="after")
    def _check_source(self) -> ToolCreate:
        if self.source_type == "http":
            if self.config is None:
                raise _invalid("HTTP tools require a config")
        elif self.config is not None:
            raise _invalid("native tools take their definition from the native registry")
        return self


class ToolUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=160)
    description: str | None = None
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    risk_level: RiskLevel | None = None
    timeout_seconds: int | None = Field(default=None, ge=1, le=600)
    permission_scope: list[str] | None = None
    requires_approval: bool | None = None
    enabled: bool | None = None
    sensitive_fields: list[str] | None = None
    config: HttpToolConfig | None = None

    @field_validator("permission_scope")
    @classmethod
    def _scope(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _normalize_scope(value)

    @field_validator("sensitive_fields")
    @classmethod
    def _sensitive(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _normalize_fields(value)

    @field_validator("input_schema", "output_schema")
    @classmethod
    def _schema(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        return _validate_object_schema(value, "schema")


class ToolOut(BaseModel):
    id: str
    name: str
    display_name: str
    description: str
    version: str
    current_version: int
    source_type: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    risk_level: str
    timeout_seconds: int
    permission_scope: list[str] = Field(default_factory=list)
    requires_approval: bool
    enabled: bool
    config: dict[str, Any] = Field(default_factory=dict)
    sensitive_fields: list[str] = Field(default_factory=list)
    created_by: str | None = None
    created_at: datetime
    updated_at: datetime


class ToolTestRequest(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolTestResult(BaseModel):
    ok: bool
    status: str
    result: Any = None
    error: str | None = None
    duration_ms: int = 0
    masked_arguments: dict[str, Any] = Field(default_factory=dict)


class ToolVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tool_id: str
    version: int
    snapshot: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class ToolExecutionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tool_id: str | None = None
    tool_name: str
    execution_id: str | None = None
    user_id: str
    agent_id: str | None = None
    arguments_masked: dict[str, Any] = Field(default_factory=dict)
    result: Any = None
    status: str
    error: str | None = None
    duration_ms: int = 0
    created_at: datetime
