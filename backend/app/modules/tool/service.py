from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping, Sequence
from typing import Any

import httpx
from jsonschema import Draft202012Validator
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.contracts import (
    ToolContext,
    ToolDescriptor,
    ToolExecutionResult,
    ToolGateway,
    ToolInvocation,
    UserContext,
)
from app.core.enums import ToolExecutionStatus, ToolSourceType
from app.core.exceptions import (
    AppError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from app.core.logging import get_logger
from app.core.policy import (
    PolicyEffect,
    allow,
    decide_tool_access,
    decide_tool_call,
)
from app.modules.tool.adapters.http import execute_http_tool
from app.modules.tool.exceptions import (
    ToolDefinitionError,
    ToolExecutionError,
    ToolTimeoutError,
)
from app.modules.tool.mcp_client import call_mcp_tool
from app.modules.tool.models import (
    MCPServer,
    Tool,
    ToolExecution,
    ToolPermission,
    ToolVersion,
)
from app.modules.tool.registry import native_registry
from app.modules.tool.repository import ToolRepository
from app.modules.tool.schemas import (
    HttpToolConfig,
    ToolCreate,
    ToolExecutionOut,
    ToolOut,
    ToolTestResult,
    ToolUpdate,
)

GRANT_ACTIONS = {"execute", "*", "tools:execute"}

logger = get_logger(__name__)


def _canonical(snapshot: Mapping[str, Any] | None) -> str:
    return json.dumps(snapshot or {}, sort_keys=True, separators=(",", ":"), default=str)


def mask_arguments(arguments: Mapping[str, Any], sensitive_fields: Sequence[str]) -> dict[str, Any]:
    """Recursively replace sensitive argument names before anything is persisted."""

    sensitive = set(sensitive_fields or [])

    def _walk(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                key: "***" if key in sensitive else _walk(item) for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [_walk(item) for item in value]
        return value

    return _walk(arguments)


def json_safe(value: Any) -> Any:
    """Normalize arbitrary handler output into a JSON-serializable value."""

    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_safe(item) for item in value]
    if isinstance(value, BaseModel):
        return json_safe(value.model_dump(mode="json"))
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return str(value)[:4000]


class ToolService(ToolGateway):
    """Tool registry, execution gate and execution recorder.

    This service is the single execution path for tools: schema validation,
    permission scope, approval gate, timeout and masking all live here.
    """

    def __init__(
        self,
        db: AsyncSession,
        *,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.db = db
        self.repo = ToolRepository(db)
        self.http_transport = http_transport

    # ------------------------------------------------------------------ queries

    async def list_tools(self, actor: UserContext) -> list[ToolOut]:
        tools = await self.repo.list_tools()
        versions = await self.repo.latest_versions()
        redact = not actor.is_superuser
        return [
            self._out(tool, versions.get(tool.id, 0), redact_config=redact)
            for tool in tools
        ]

    async def get_tool(
        self, tool_id_or_name: str, actor: UserContext | None = None
    ) -> ToolOut:
        tool = await self._load(tool_id_or_name)
        redact = actor is not None and not actor.is_superuser
        return await self._to_out(tool, redact_config=redact)

    async def describe(
        self, names: Sequence[str], user: UserContext
    ) -> list[ToolDescriptor]:
        requested = list(dict.fromkeys(name for name in names if name))
        if not requested:
            return []
        rows = await self.repo.get_many_by_names(requested)
        missing = [name for name in requested if name not in rows or not rows[name].enabled]
        if missing:
            raise ValidationError(
                "Unknown or disabled tools: " + ", ".join(missing),
                details={"missing": missing},
            )
        grants = await self.repo.grants_for_tools([rows[name].id for name in requested])
        descriptors: list[ToolDescriptor] = []
        for name in requested:
            tool = rows[name]
            decision = self._decide_access(tool, user, grants.get(tool.id, []))
            if decision.allowed:
                descriptors.append(self._descriptor(tool))
        return descriptors

    # ------------------------------------------------------------- registration

    async def sync_native(self) -> int:
        """Upsert every registered native descriptor, versioning real changes."""

        synced = 0
        for entry in native_registry.all():
            descriptor = entry.descriptor
            existing = await self.repo.get_by_name(descriptor.name)
            if existing is None:
                tool = Tool(
                    name=descriptor.name,
                    display_name=descriptor.name,
                    description=descriptor.description,
                    version=descriptor.version,
                    source_type=ToolSourceType.NATIVE.value,
                    input_schema=descriptor.input_schema,
                    output_schema=descriptor.output_schema,
                    risk_level=descriptor.risk_level,
                    timeout_seconds=descriptor.timeout_seconds,
                    permission_scope=list(descriptor.permission_scope),
                    requires_approval=descriptor.requires_approval,
                    enabled=descriptor.enabled,
                    config={},
                    sensitive_fields=[],
                    created_by=None,
                )
                self.db.add(tool)
                await self.db.flush()
                self.repo.add_version(
                    ToolVersion(tool_id=tool.id, version=1, snapshot=self._row_snapshot(tool))
                )
            else:
                if existing.source_type != ToolSourceType.NATIVE.value:
                    # A same-named HTTP/MCP tool already exists. Startup must not
                    # fail on that, and silently overwriting an operator-defined
                    # tool would be worse, so keep the existing row untouched.
                    logger.warning(
                        "native tool name is taken by another source type; skipping sync",
                        extra={
                            "event": "tool_sync_conflict",
                            "tool": descriptor.name,
                            "source_type": existing.source_type,
                        },
                    )
                    continue
                latest = await self.repo.latest_version(existing.id)
                self._apply_descriptor(existing, descriptor)
                await self.db.flush()
                snapshot = self._row_snapshot(existing)
                if latest is None or _canonical(latest.snapshot) != _canonical(snapshot):
                    next_version = (latest.version if latest else 0) + 1
                    self.repo.add_version(
                        ToolVersion(
                            tool_id=existing.id, version=next_version, snapshot=snapshot
                        )
                    )
            synced += 1
        await self.db.commit()
        return synced

    async def create_tool(self, payload: ToolCreate, actor: UserContext) -> ToolOut:
        if await self.repo.get_by_name(payload.name) is not None:
            raise ConflictError(f"Tool {payload.name} already exists")
        if payload.source_type == ToolSourceType.HTTP.value:
            tool = self._build_http_tool(payload, actor)
        else:
            tool = self._build_native_tool(payload, actor)
        self.db.add(tool)
        await self.db.flush()
        self.repo.add_version(
            ToolVersion(tool_id=tool.id, version=1, snapshot=self._row_snapshot(tool))
        )
        await self.db.commit()
        return await self._to_out(tool)

    async def update_tool(
        self, tool_id: str, payload: ToolUpdate, actor: UserContext
    ) -> ToolOut:
        tool = await self._load(tool_id)
        data = payload.model_dump(exclude_unset=True)
        for key in ("input_schema", "output_schema", "config"):
            if data.get(key) is None:
                data.pop(key, None)
        if tool.source_type == ToolSourceType.NATIVE.value:
            # Native descriptors are code-owned. Only operator-friendly metadata
            # may change without a deployment; security policy is immutable.
            protected = {
                "input_schema",
                "output_schema",
                "risk_level",
                "timeout_seconds",
                "permission_scope",
                "requires_approval",
                "config",
            }
            blocked = sorted(protected.intersection(data))
            if blocked:
                raise ToolDefinitionError(
                    f"Native tool security fields are code-owned: {', '.join(blocked)}"
                )
        if tool.source_type == ToolSourceType.MCP.value:
            # MCP tools mirror the remote server. Schemas and the discovery
            # binding are owned by sync; operators tune policy metadata only.
            protected = {"input_schema", "output_schema", "config"}
            blocked = sorted(protected.intersection(data))
            if blocked:
                raise ToolDefinitionError(
                    "MCP tool schemas and config are owned by server sync: "
                    + ", ".join(blocked)
                )
        if "config" in data:
            data["config"] = HttpToolConfig.model_validate(data["config"]).model_dump(
                mode="json"
            )

        behavior_keys = (
            "source_type",
            "input_schema",
            "output_schema",
            "risk_level",
            "timeout_seconds",
            "permission_scope",
            "requires_approval",
            "config",
            "sensitive_fields",
        )

        def behavior_snapshot() -> str:
            snapshot = self._row_snapshot(tool)
            return _canonical({key: snapshot.get(key) for key in behavior_keys})

        before = behavior_snapshot()
        for key, value in data.items():
            setattr(tool, key, value)
        await self.db.flush()
        if behavior_snapshot() != before:
            latest = await self.repo.latest_version(tool.id)
            next_version = (latest.version if latest else 0) + 1
            if tool.source_type == ToolSourceType.HTTP.value:
                tool.version = str(next_version)
            self.repo.add_version(
                ToolVersion(
                    tool_id=tool.id,
                    version=next_version,
                    snapshot=self._row_snapshot(tool),
                )
            )
            await self.db.flush()
        await self.db.commit()
        return await self._to_out(tool)

    async def delete_tool(self, tool_id: str, actor: UserContext | None = None) -> None:
        tool = await self._load(tool_id)
        if tool.source_type == ToolSourceType.NATIVE.value:
            raise ToolDefinitionError("Native tools are managed by code and cannot be deleted")
        if tool.source_type == ToolSourceType.MCP.value:
            raise ToolDefinitionError(
                "MCP tools are managed by server sync; remove the tool remotely or "
                "delete/disable the MCP server instead"
            )
        await self.db.delete(tool)
        await self.db.commit()

    async def set_enabled(self, tool_id: str, enabled: bool, actor: UserContext) -> ToolOut:
        tool = await self._load(tool_id)
        tool.enabled = enabled
        await self.db.commit()
        return await self._to_out(tool)

    # ------------------------------------------------------------------ execute

    async def execute(
        self, invocation: ToolInvocation, user: UserContext
    ) -> ToolExecutionResult:
        if not user.has("tools:execute"):
            raise PermissionDeniedError("Missing permission: tools:execute")
        tool = await self.repo.get_by_name(invocation.tool_name)
        if tool is None or not tool.enabled:
            raise NotFoundError(f"Tool {invocation.tool_name} not found or disabled")
        return await self._run(
            tool,
            invocation.arguments,
            user,
            execution_id=invocation.execution_id,
            agent_id=invocation.agent_id,
            approved=invocation.approved,
            tool_call_id=invocation.tool_call_id,
        )

    async def list_execution_records(self, execution_id: str) -> list[ToolExecutionOut]:
        """Read-only execution history; callers must enforce execution access."""

        rows = await self.repo.list_execution_records(execution_id)
        return [ToolExecutionOut.model_validate(row) for row in rows]

    async def test_tool(
        self, tool_id_or_name: str, arguments: dict[str, Any], actor: UserContext
    ) -> ToolTestResult:
        missing = [code for code in ("tools:write", "tools:execute") if not actor.has(code)]
        if missing:
            raise PermissionDeniedError(f"Missing permission: {', '.join(missing)}")
        tool = await self._load(tool_id_or_name)
        masked = mask_arguments(arguments, tool.sensitive_fields or [])
        decision = decide_tool_call(self._descriptor(tool), bound=True, access_allowed=True)
        if decision.effect is PolicyEffect.REQUIRE_APPROVAL:
            started = time.perf_counter()
            result = await self._finish(
                tool,
                actor,
                None,
                None,
                masked,
                ToolExecutionStatus.DENIED,
                "high_risk_test_requires_approval",
                None,
                started,
            )
            return self._test_result(result)
        result = await self._run(tool, arguments, actor, approved=False)
        return self._test_result(result)

    async def _run(
        self,
        tool: Tool,
        arguments: dict[str, Any],
        user: UserContext,
        *,
        execution_id: str | None = None,
        agent_id: str | None = None,
        approved: bool = False,
        enforce_scope: bool = True,
        tool_call_id: str | None = None,
    ) -> ToolExecutionResult:
        started = time.perf_counter()
        masked = mask_arguments(arguments, tool.sensitive_fields or [])

        if execution_id and tool_call_id:
            # A retried step must not repeat a side effect. Replay the recorded
            # successful result instead of invoking the handler again.
            replayed = await self.repo.get_execution_by_call(execution_id, tool_call_id)
            if (
                replayed is not None
                and replayed.status == ToolExecutionStatus.SUCCESS.value
                and replayed.tool_name == tool.name
                and dict(replayed.arguments_masked or {}) == masked
            ):
                return ToolExecutionResult(
                    ok=True,
                    status=ToolExecutionStatus.SUCCESS.value,
                    output=replayed.result,
                    error=None,
                    duration_ms=0,
                    masked_arguments=masked,
                    replayed=True,
                )

        if enforce_scope:
            grants = await self.repo.grants_for_tool(tool.id)
            access = self._decide_access(tool, user, grants)
            if not access.allowed:
                return await self._finish(
                    tool,
                    user,
                    execution_id,
                    agent_id,
                    masked,
                    ToolExecutionStatus.DENIED,
                    access.code,
                    None,
                    started,
                    tool_call_id=tool_call_id,
                )

        approval = decide_tool_call(
            self._descriptor(tool), bound=True, access_allowed=True
        )
        if not approved and approval.effect is PolicyEffect.REQUIRE_APPROVAL:
            return await self._finish(
                tool,
                user,
                execution_id,
                agent_id,
                masked,
                ToolExecutionStatus.DENIED,
                "approval_required",
                None,
                started,
                tool_call_id=tool_call_id,
            )

        validation_error = self._validate_arguments(tool, arguments)
        if validation_error:
            return await self._finish(
                tool,
                user,
                execution_id,
                agent_id,
                masked,
                ToolExecutionStatus.FAILED,
                self._scrub(validation_error, arguments, tool.sensitive_fields or []),
                None,
                started,
                tool_call_id=tool_call_id,
            )

        timeout_seconds = self._timeout(tool)
        try:
            output = await asyncio.wait_for(
                self._invoke(tool, arguments, user, execution_id, agent_id, timeout_seconds),
                timeout=timeout_seconds,
            )
        except (TimeoutError, ToolTimeoutError) as exc:
            error = (
                str(exc)
                if isinstance(exc, ToolTimeoutError)
                else f"Tool execution timed out after {timeout_seconds}s"
            )
            return await self._finish(
                tool,
                user,
                execution_id,
                agent_id,
                masked,
                ToolExecutionStatus.TIMEOUT,
                error,
                None,
                started,
                tool_call_id=tool_call_id,
            )
        except asyncio.CancelledError:
            await self._finish(
                tool,
                user,
                execution_id,
                agent_id,
                masked,
                ToolExecutionStatus.CANCELLED,
                "cancelled",
                None,
                started,
                tool_call_id=tool_call_id,
            )
            raise
        except Exception as exc:  # noqa: BLE001 - tool failures become recorded results
            return await self._finish(
                tool,
                user,
                execution_id,
                agent_id,
                masked,
                ToolExecutionStatus.FAILED,
                self._scrub(
                    self._error_text(exc), arguments, tool.sensitive_fields or []
                ),
                None,
                started,
                tool_call_id=tool_call_id,
            )

        return await self._finish(
            tool,
            user,
            execution_id,
            agent_id,
            masked,
            ToolExecutionStatus.SUCCESS,
            None,
            json_safe(output),
            started,
            tool_call_id=tool_call_id,
        )

    async def _invoke(
        self,
        tool: Tool,
        arguments: dict[str, Any],
        user: UserContext,
        execution_id: str | None,
        agent_id: str | None,
        timeout_seconds: float,
    ) -> Any:
        if tool.source_type == ToolSourceType.NATIVE.value:
            entry = native_registry.get(tool.name)
            if entry is None:
                raise ToolExecutionError(
                    f"Native handler for tool {tool.name} is not registered in this process"
                )
            context = ToolContext(
                user=user,
                tool_name=tool.name,
                execution_id=execution_id,
                agent_id=agent_id,
            )
            return await entry.handler(dict(arguments), context)
        if tool.source_type == ToolSourceType.HTTP.value:
            return await execute_http_tool(
                tool.config or {},
                dict(arguments),
                transport=self.http_transport,
                timeout_seconds=timeout_seconds,
            )
        if tool.source_type == ToolSourceType.MCP.value:
            config = tool.config or {}
            server_id = config.get("server_id")
            remote_name = config.get("remote_name")
            if not isinstance(server_id, str) or not server_id:
                raise ToolExecutionError(f"MCP tool {tool.name} has no server binding")
            if not isinstance(remote_name, str) or not remote_name:
                raise ToolExecutionError(f"MCP tool {tool.name} has no remote tool name")
            server = await self.db.get(MCPServer, server_id)
            if server is None:
                raise ToolExecutionError(
                    f"MCP server for tool {tool.name} no longer exists"
                )
            if not server.enabled:
                raise ToolExecutionError(f"MCP server {server.name} is disabled")
            return await call_mcp_tool(
                server, remote_name, dict(arguments), timeout_seconds
            )
        raise ToolExecutionError(f"Unsupported tool source type: {tool.source_type}")

    async def _finish(
        self,
        tool: Tool,
        user: UserContext,
        execution_id: str | None,
        agent_id: str | None,
        masked: dict[str, Any],
        status: ToolExecutionStatus,
        error: str | None,
        output: Any,
        started: float,
        tool_call_id: str | None = None,
    ) -> ToolExecutionResult:
        duration_ms = max(0, int((time.perf_counter() - started) * 1000))
        self.repo.record_execution(
            ToolExecution(
                tool_id=tool.id,
                tool_name=tool.name,
                execution_id=execution_id,
                tool_call_id=tool_call_id,
                user_id=user.user_id,
                agent_id=agent_id,
                arguments_masked=masked,
                result=output,
                status=status.value,
                error=error,
                duration_ms=duration_ms,
            )
        )
        await self.db.commit()
        return ToolExecutionResult(
            ok=status == ToolExecutionStatus.SUCCESS,
            status=status.value,
            output=output,
            error=error,
            duration_ms=duration_ms,
            masked_arguments=masked,
        )

    # ------------------------------------------------------------------- helpers

    def _decide_access(
        self, tool: Tool, user: UserContext, grants: Sequence[ToolPermission]
    ):
        if user.is_superuser:
            return allow("superuser")
        scope = list(tool.permission_scope or [])
        granted = any(
            self._grant_matches(grant, user)
            and bool(set(grant.actions or []) & GRANT_ACTIONS)
            for grant in grants
        )
        return decide_tool_access(
            enabled=tool.enabled,
            required_scopes=scope,
            has_all_scopes=all(user.has(code) for code in scope),
            granted=granted,
        )

    @staticmethod
    def _grant_matches(grant: ToolPermission, user: UserContext) -> bool:
        if grant.subject_type == "user":
            return grant.subject_id == user.user_id
        if grant.subject_type == "role":
            return grant.subject_id in user.roles
        return False

    def _validate_arguments(self, tool: Tool, arguments: dict[str, Any]) -> str | None:
        if not isinstance(arguments, dict):
            return "Invalid arguments: expected a JSON object"
        schema = tool.input_schema or {}
        if not schema:
            return None
        validator = Draft202012Validator(schema)
        errors = sorted(
            validator.iter_errors(arguments), key=lambda error: list(error.absolute_path)
        )
        if not errors:
            return None
        error = errors[0]
        location = ".".join(str(part) for part in error.absolute_path) or "<root>"
        return f"Invalid arguments at {location}: {error.message}"

    @staticmethod
    def _error_text(exc: Exception) -> str:
        if isinstance(exc, AppError):
            text = exc.message
        else:
            text = f"{type(exc).__name__}: {exc}"
        return text[:1000]

    @staticmethod
    def _scrub(
        text: str, arguments: dict[str, Any], sensitive_fields: Sequence[str]
    ) -> str:
        sensitive = set(sensitive_fields or [])
        if not sensitive:
            return text

        def _collect(value: Any) -> list[str]:
            found: list[str] = []
            if isinstance(value, Mapping):
                for key, item in value.items():
                    if key in sensitive and isinstance(item, str | int | float):
                        found.append(str(item))
                    else:
                        found.extend(_collect(item))
            elif isinstance(value, (list, tuple)):
                for item in value:
                    found.extend(_collect(item))
            return found

        for secret in _collect(arguments):
            if secret:
                text = text.replace(secret, "***")
        return text

    @staticmethod
    def _timeout(tool: Tool) -> float:
        timeout = tool.timeout_seconds or 60
        config = tool.config or {}
        if tool.source_type == ToolSourceType.HTTP.value and config.get("timeout_seconds"):
            timeout = config["timeout_seconds"]
        try:
            return max(1.0, float(timeout))
        except (TypeError, ValueError):
            return 60.0

    def _build_native_tool(self, payload: ToolCreate, actor: UserContext) -> Tool:
        entry = native_registry.get(payload.name)
        if entry is None:
            raise ToolDefinitionError(
                f"Native tool '{payload.name}' is not registered in this process"
            )
        descriptor = entry.descriptor
        return Tool(
            name=descriptor.name,
            display_name=payload.display_name or descriptor.name,
            description=payload.description or descriptor.description,
            version=descriptor.version,
            source_type=ToolSourceType.NATIVE.value,
            input_schema=descriptor.input_schema,
            output_schema=(
                payload.output_schema
                if payload.output_schema is not None
                else descriptor.output_schema
            ),
            risk_level=payload.risk_level or descriptor.risk_level,
            timeout_seconds=payload.timeout_seconds or descriptor.timeout_seconds,
            permission_scope=(
                list(payload.permission_scope)
                if payload.permission_scope is not None
                else list(descriptor.permission_scope)
            ),
            requires_approval=(
                payload.requires_approval
                if payload.requires_approval is not None
                else descriptor.requires_approval
            ),
            enabled=payload.enabled and descriptor.enabled,
            config={},
            sensitive_fields=list(payload.sensitive_fields),
            created_by=actor.user_id,
        )

    def _build_http_tool(self, payload: ToolCreate, actor: UserContext) -> Tool:
        if payload.config is None:  # pragma: no cover - schema guarantees this
            raise ToolDefinitionError("HTTP tools require a config")
        return Tool(
            name=payload.name,
            display_name=payload.display_name or payload.name,
            description=payload.description,
            version="1",
            source_type=ToolSourceType.HTTP.value,
            input_schema=payload.input_schema
            if payload.input_schema is not None
            else {"type": "object", "properties": {}},
            output_schema=payload.output_schema or {},
            risk_level=payload.risk_level or "low",
            timeout_seconds=payload.timeout_seconds or 60,
            permission_scope=list(payload.permission_scope or []),
            requires_approval=bool(payload.requires_approval),
            enabled=payload.enabled,
            config=payload.config.model_dump(mode="json"),
            sensitive_fields=list(payload.sensitive_fields),
            created_by=actor.user_id,
        )

    @staticmethod
    def _apply_descriptor(tool: Tool, descriptor: ToolDescriptor) -> None:
        tool.description = descriptor.description
        tool.input_schema = descriptor.input_schema
        tool.output_schema = descriptor.output_schema
        tool.risk_level = descriptor.risk_level
        tool.timeout_seconds = descriptor.timeout_seconds
        tool.permission_scope = list(descriptor.permission_scope)
        tool.requires_approval = descriptor.requires_approval
        tool.version = descriptor.version
        if not tool.display_name:
            tool.display_name = descriptor.name

    @staticmethod
    def _row_snapshot(tool: Tool) -> dict[str, Any]:
        return {
            "source_type": tool.source_type,
            "description": tool.description,
            "input_schema": tool.input_schema or {},
            "output_schema": tool.output_schema or {},
            "risk_level": tool.risk_level,
            "timeout_seconds": tool.timeout_seconds,
            "permission_scope": list(tool.permission_scope or []),
            "requires_approval": tool.requires_approval,
            "config": tool.config or {},
            "sensitive_fields": list(tool.sensitive_fields or []),
            "version": tool.version,
        }

    @staticmethod
    def _descriptor(tool: Tool) -> ToolDescriptor:
        return ToolDescriptor(
            name=tool.name,
            description=tool.description,
            input_schema=tool.input_schema or {},
            output_schema=tool.output_schema or {},
            version=tool.version,
            source_type=tool.source_type,
            risk_level=tool.risk_level,
            timeout_seconds=tool.timeout_seconds,
            permission_scope=list(tool.permission_scope or []),
            requires_approval=tool.requires_approval,
            enabled=tool.enabled,
            sensitive_fields=list(tool.sensitive_fields or []),
        )

    async def _load(self, tool_id_or_name: str) -> Tool:
        tool = await self.repo.get(tool_id_or_name)
        if tool is None:
            tool = await self.repo.get_by_name(tool_id_or_name)
        if tool is None:
            raise NotFoundError(f"Tool {tool_id_or_name} not found")
        return tool

    async def _to_out(self, tool: Tool, *, redact_config: bool = False) -> ToolOut:
        latest = await self.repo.latest_version(tool.id)
        return self._out(tool, latest.version if latest else 0, redact_config=redact_config)

    @staticmethod
    def _out(
        tool: Tool, current_version: int, *, redact_config: bool = False
    ) -> ToolOut:
        return ToolOut(
            id=tool.id,
            name=tool.name,
            display_name=tool.display_name,
            description=tool.description,
            version=tool.version,
            current_version=current_version,
            source_type=tool.source_type,
            input_schema=tool.input_schema or {},
            output_schema=tool.output_schema or {},
            risk_level=tool.risk_level,
            timeout_seconds=tool.timeout_seconds,
            permission_scope=list(tool.permission_scope or []),
            requires_approval=tool.requires_approval,
            enabled=tool.enabled,
            config={} if redact_config else (tool.config or {}),
            sensitive_fields=list(tool.sensitive_fields or []),
            created_by=tool.created_by,
            created_at=tool.created_at,
            updated_at=tool.updated_at,
        )

    @staticmethod
    def _test_result(result: ToolExecutionResult) -> ToolTestResult:
        return ToolTestResult(
            ok=result.ok,
            status=result.status,
            result=result.output,
            error=result.error,
            duration_ms=result.duration_ms,
            masked_arguments=result.masked_arguments,
        )
