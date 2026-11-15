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
ToolSource = Literal["native", "http", "mcp"]
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
        if self.source_type == "mcp":
            raise _invalid(
                "MCP tools are created by MCP server sync; register an MCP server instead"
            )
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


# ---------------------------------------------------------------------------
# MCP servers
# ---------------------------------------------------------------------------

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SECRET_ENV_KEY = re.compile(
    r"(TOKEN|SECRET|PASSWORD|PASSWD|API[_-]?KEY|CREDENTIAL|PRIVATE[_-]?KEY|ACCESS[_-]?KEY)",
    re.IGNORECASE,
)
_HEADER_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_MAX_ENV_ENTRIES = 64
_MAX_ARGS = 64
_MAX_ARG_CHARS = 2000


def _check_secret_mapping(value: dict[str, str], label: str) -> dict[str, str]:
    """Validate literal-or-``${TOOL_SECRET_*}`` mappings for env/headers."""

    if not isinstance(value, dict):
        raise _invalid(f"{label} must be an object mapping names to strings")
    prefix = get_settings().tool_secret_env_prefix
    cleaned: dict[str, str] = {}
    for raw_name, raw_value in value.items():
        name = (raw_name or "").strip()
        if not name:
            raise _invalid(f"{label} names must be non-empty strings")
        if not isinstance(raw_value, str):
            raise _invalid(f"{label} values must be strings")
        if "\n" in raw_value or "\r" in raw_value:
            raise _invalid(f"{label} {name} must not contain newlines")
        if "\x00" in raw_value:
            raise _invalid(f"{label} {name} must not contain NUL bytes")
        if label == "env" and not _ENV_NAME.match(name):
            raise _invalid(
                f"env name {name!r} must match {_ENV_NAME.pattern}"
            )
        if label == "headers" and not _HEADER_NAME.match(name):
            raise _invalid(f"header name {name!r} is not a valid HTTP header name")
        refs = ENV_REF_PATTERN.findall(raw_value)
        for ref in refs:
            if not ref.startswith(prefix):
                raise _invalid(
                    f"{label} {name} references {ref}; environment refs must start with "
                    f"{prefix}"
                )
        literal = ENV_REF_PATTERN.sub("", raw_value).strip()
        if literal and any(pattern.match(literal) for pattern in _SECRET_LIKE):
            raise _invalid(
                f"{label} {name} looks like a literal secret; use a ${{{prefix}...}} reference"
            )
        if not refs:
            if label == "headers" and name.lower() in SENSITIVE_HEADERS:
                raise _invalid(
                    f"header {name} must reference an environment variable, e.g. "
                    f"${{{prefix}{name.upper().replace('-', '_')}}}"
                )
            if label == "env" and _SECRET_ENV_KEY.search(name):
                raise _invalid(
                    f"env {name} must reference an environment variable, e.g. "
                    f"${{{prefix}{name.upper()}}}"
                )
        cleaned[name] = raw_value
    if len(cleaned) > _MAX_ENV_ENTRIES:
        raise _invalid(f"{label} may contain at most {_MAX_ENV_ENTRIES} entries")
    return cleaned


def _check_args(value: list[str]) -> list[str]:
    if len(value) > _MAX_ARGS:
        raise _invalid(f"args may contain at most {_MAX_ARGS} entries")
    cleaned: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise _invalid("args entries must be strings")
        if "\x00" in item:
            raise _invalid("args entries must not contain NUL bytes")
        if len(item) > _MAX_ARG_CHARS:
            raise _invalid(f"args entries may be at most {_MAX_ARG_CHARS} characters")
        cleaned.append(item)
    return cleaned


def _check_mcp_url(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        raise _invalid("url must not be empty")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise _invalid("url is not a valid absolute URL") from exc
    if parts.scheme not in {"http", "https"}:
        raise _invalid("url must use the http or https scheme")
    if not parts.hostname:
        raise _invalid("url must include a host")
    if parts.username or parts.password:
        raise _invalid("url must not embed credentials; use ${TOOL_SECRET_*} headers")
    if port is not None and not (0 < port < 65536):
        raise _invalid("url port is out of range")
    return value


class MCPServerBase(BaseModel):
    """Transport-neutral MCP server configuration."""

    model_config = ConfigDict(extra="forbid")

    transport: Literal["stdio", "http"] = "stdio"
    command: str | None = Field(default=None, max_length=500)
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = Field(default=None, max_length=2000)
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True

    @field_validator("command")
    @classmethod
    def _command(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise _invalid("stdio transport requires a non-empty command")
        if "\x00" in value:
            raise _invalid("command must not contain NUL bytes")
        return value

    @field_validator("args")
    @classmethod
    def _args(cls, value: list[str]) -> list[str]:
        return _check_args(value)

    @field_validator("env")
    @classmethod
    def _env(cls, value: dict[str, str]) -> dict[str, str]:
        return _check_secret_mapping(value, "env")

    @field_validator("headers")
    @classmethod
    def _headers(cls, value: dict[str, str]) -> dict[str, str]:
        return _check_secret_mapping(value, "headers")

    @field_validator("url")
    @classmethod
    def _url(cls, value: str | None) -> str | None:
        return _check_mcp_url(value)

    @model_validator(mode="after")
    def _check_transport(self) -> MCPServerBase:
        if self.transport == "stdio":
            if not self.command:
                raise _invalid("stdio transport requires a command")
            if self.url is not None:
                raise _invalid("stdio transport must not set url")
            if self.headers:
                raise _invalid("stdio transport must not set headers")
        else:
            if not self.url:
                raise _invalid("http transport requires a url")
            if self.command is not None:
                raise _invalid("http transport must not set command")
            if self.args:
                raise _invalid("http transport must not set args")
            if self.env:
                raise _invalid("http transport must not set env")
        return self


class MCPServerCreate(MCPServerBase):
    name: str = Field(min_length=2, max_length=120, pattern=NAME_PATTERN)


class MCPServerUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transport: Literal["stdio", "http"] | None = None
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    url: str | None = None
    headers: dict[str, str] | None = None
    enabled: bool | None = None

    @field_validator("args")
    @classmethod
    def _args(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _check_args(value)

    @field_validator("env")
    @classmethod
    def _env(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        return None if value is None else _check_secret_mapping(value, "env")

    @field_validator("headers")
    @classmethod
    def _headers(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        return None if value is None else _check_secret_mapping(value, "headers")

    @field_validator("url")
    @classmethod
    def _url(cls, value: str | None) -> str | None:
        return None if value is None else _check_mcp_url(value)


def redact_secret_mapping(values: dict[str, str] | None) -> dict[str, str]:
    """Hide literal values; keep pure ``${...}`` references visible."""

    redacted: dict[str, str] = {}
    for name, value in (values or {}).items():
        if ENV_REF_PATTERN.search(value) and not ENV_REF_PATTERN.sub("", value).strip():
            redacted[name] = value
        else:
            redacted[name] = "***"
    return redacted


class MCPServerOut(BaseModel):
    id: str
    name: str
    transport: str
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool
    last_sync_at: datetime | None = None
    last_error: str | None = None
    created_by: str | None = None
    created_at: datetime
    updated_at: datetime


class MCPSyncResult(BaseModel):
    server_id: str
    server_name: str
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    disabled: int = 0
    tools: list[ToolOut] = Field(default_factory=list)
