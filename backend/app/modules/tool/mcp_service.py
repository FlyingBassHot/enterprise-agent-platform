"""Admin-only MCP server registry and tool discovery.

``MCPService`` owns MCP server CRUD and the discovery/upsert of remote tools
into the existing ``tools`` registry. Discovered tools are ordinary ``Tool``
rows with ``source_type="mcp"`` and are executed by ``ToolService`` through the
MCP client, so permissions, approval gates, timeouts and execution records stay
on the single execution path.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.contracts import UserContext
from app.core.enums import ToolSourceType
from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ToolError,
)
from app.modules.tool.exceptions import ToolDefinitionError, ToolExecutionError
from app.modules.tool.mcp_client import MCPToolDefinition, list_mcp_tools
from app.modules.tool.models import MCPServer, Tool, ToolVersion
from app.modules.tool.schemas import (
    MCPServerCreate,
    MCPServerOut,
    MCPServerUpdate,
    MCPSyncResult,
    ToolOut,
    _validate_object_schema,
    redact_secret_mapping,
)
from app.modules.tool.service import ToolService

MAX_TOOL_NAME = 120
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9_.:-]")


def _canonical(value: Any) -> str:
    return json.dumps(value or {}, sort_keys=True, separators=(",", ":"), default=str)


def _sanitize_segment(value: str) -> str:
    cleaned = _UNSAFE_NAME_CHARS.sub("_", (value or "").strip())
    if not cleaned or not cleaned[0].isalnum():
        cleaned = f"t_{cleaned}"
    return cleaned


def synced_tool_name(server_name: str, remote_name: str) -> str:
    """Deterministic, collision-resistant registry name for a remote tool."""

    base = f"mcp__{server_name}__{_sanitize_segment(remote_name)}"
    if len(base) <= MAX_TOOL_NAME:
        return base
    digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:8]
    return f"{base[: MAX_TOOL_NAME - len(digest) - 1]}_{digest}"


class MCPService:
    """MCP server administration; every public method requires ``tools:admin``."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.settings = get_settings()

    # ------------------------------------------------------------------ servers

    async def list_servers(self, actor: UserContext) -> list[MCPServerOut]:
        self._require_admin(actor)
        servers = list(
            (await self.db.scalars(select(MCPServer).order_by(MCPServer.name))).all()
        )
        return [self._out(server, actor) for server in servers]

    async def get_server(self, server_id: str, actor: UserContext) -> MCPServerOut:
        self._require_admin(actor)
        return self._out(await self._load(server_id), actor)

    async def create_server(
        self, payload: MCPServerCreate, actor: UserContext
    ) -> MCPServerOut:
        self._require_admin(actor)
        if await self._get_by_name(payload.name) is not None:
            raise ConflictError(f"MCP server {payload.name} already exists")
        values = payload.model_dump(mode="json")
        name = values.pop("name")
        server = MCPServer(name=name, created_by=actor.user_id, **values)
        self.db.add(server)
        await self.db.commit()
        await self.db.refresh(server)
        return self._out(server, actor)

    async def update_server(
        self, server_id: str, payload: MCPServerUpdate, actor: UserContext
    ) -> MCPServerOut:
        self._require_admin(actor)
        server = await self._load(server_id)
        patch = payload.model_dump(exclude_unset=True)
        if not patch:
            return self._out(server, actor)

        merged: dict[str, Any] = {
            "transport": server.transport,
            "command": server.command,
            "args": list(server.args or []),
            "env": dict(server.env or {}),
            "url": server.url,
            "headers": dict(server.headers or {}),
            "enabled": server.enabled,
        }
        transport = patch.get("transport", server.transport)
        if transport != server.transport:
            # Switching transport must not leave fields from the other one.
            if transport == "http":
                merged.update(command=None, args=[], env={})
            else:
                merged.update(url=None, headers={})
        merged.update(patch)
        validated = MCPServerCreate(name=server.name, **merged)
        for key, value in validated.model_dump(mode="json").items():
            if key != "name":
                setattr(server, key, value)
        server.last_error = None
        await self.db.commit()
        await self.db.refresh(server)
        return self._out(server, actor)

    async def delete_server(self, server_id: str, actor: UserContext) -> None:
        self._require_admin(actor)
        server = await self._load(server_id)
        for tool in await self._server_tools(server.id):
            await self.db.delete(tool)
        await self.db.delete(server)
        await self.db.commit()

    async def list_server_tools(self, server_id: str, actor: UserContext) -> list[ToolOut]:
        self._require_admin(actor)
        server = await self._load(server_id)
        tools = await self._server_tools(server.id)
        versions = await self._latest_versions()
        return [self._tool_out(tool, versions.get(tool.id, 0)) for tool in tools]

    # --------------------------------------------------------------------- sync

    async def sync(self, server_id: str, actor: UserContext) -> MCPSyncResult:
        """Discover remote tools and upsert them into the tool registry."""

        self._require_admin(actor)
        server = await self._load(server_id)

        try:
            remote = await list_mcp_tools(
                server, timeout=float(self.settings.mcp_call_timeout_seconds or 60)
            )
        except ToolError as exc:
            await self._record_sync_error(server, exc.message)
            raise
        except Exception as exc:  # noqa: BLE001 - unknown SDK failure becomes a tool error
            error = ToolExecutionError(
                f"MCP server {server.name} sync failed: {str(exc)[:1000]}"
            )
            await self._record_sync_error(server, error.message)
            raise error from exc

        try:
            prepared = self._prepare(server, remote)
            conflicts = await self._conflicts(server, prepared)
            if conflicts:
                raise ToolDefinitionError("; ".join(conflicts))
        except ToolError as exc:
            await self._record_sync_error(server, exc.message)
            raise

        now = datetime.now(UTC)
        versions = await self._latest_versions()
        rows = list((await self.db.scalars(select(Tool).order_by(Tool.name))).all())
        synced = [
            tool
            for tool in rows
            if tool.source_type == ToolSourceType.MCP.value
            and (tool.config or {}).get("server_id") == server.id
        ]
        by_remote = {
            str((tool.config or {}).get("remote_name")): tool for tool in synced
        }

        created = updated = unchanged = 0
        matched: set[str] = set()
        result: list[Tool] = []
        for name, definition in prepared:
            tool = by_remote.get(definition.name)
            if tool is None:
                tool = Tool(
                    name=name,
                    display_name=f"{server.name}: {definition.name}"[:160],
                    description=definition.description,
                    version="1",
                    source_type=ToolSourceType.MCP.value,
                    input_schema=definition.input_schema,
                    output_schema=definition.output_schema,
                    risk_level="low",
                    timeout_seconds=int(self.settings.mcp_call_timeout_seconds or 60),
                    permission_scope=[],
                    requires_approval=False,
                    enabled=True,
                    config={"server_id": server.id, "remote_name": definition.name},
                    sensitive_fields=[],
                    created_by=actor.user_id,
                )
                self.db.add(tool)
                await self.db.flush()
                self.db.add(
                    ToolVersion(tool_id=tool.id, version=1, snapshot=ToolService._row_snapshot(tool))
                )
                versions[tool.id] = 1
                created += 1
            else:
                if tool.name != name:
                    tool.name = name
                changed = (
                    (tool.description or "") != definition.description
                    or _canonical(tool.input_schema) != _canonical(definition.input_schema)
                    or _canonical(tool.output_schema) != _canonical(definition.output_schema)
                )
                if changed:
                    tool.description = definition.description
                    tool.input_schema = definition.input_schema
                    tool.output_schema = definition.output_schema
                    next_version = versions.get(tool.id, 0) + 1
                    tool.version = str(next_version)
                    self.db.add(
                        ToolVersion(
                            tool_id=tool.id,
                            version=next_version,
                            snapshot=ToolService._row_snapshot(tool),
                        )
                    )
                    versions[tool.id] = next_version
                    updated += 1
                else:
                    unchanged += 1
                tool.enabled = True
            matched.add(tool.id)
            result.append(tool)

        disabled = 0
        for tool in synced:
            if tool.id not in matched:
                tool.enabled = False
                disabled += 1

        server.last_sync_at = now
        server.last_error = None
        await self.db.commit()
        for tool in result:
            await self.db.refresh(tool)

        return MCPSyncResult(
            server_id=server.id,
            server_name=server.name,
            created=created,
            updated=updated,
            unchanged=unchanged,
            disabled=disabled,
            tools=[self._tool_out(tool, versions.get(tool.id, 0)) for tool in result],
        )

    # ------------------------------------------------------------------ helpers

    def _prepare(
        self, server: MCPServer, remote: list[MCPToolDefinition]
    ) -> list[tuple[str, MCPToolDefinition]]:
        prepared: list[tuple[str, MCPToolDefinition]] = []
        names: dict[str, str] = {}
        remote_names: set[str] = set()
        for definition in remote:
            if definition.name in remote_names:
                raise ToolDefinitionError(
                    f"MCP server advertised duplicate tool {definition.name}"
                )
            remote_names.add(definition.name)
            name = synced_tool_name(server.name, definition.name)
            if name in names:
                raise ToolDefinitionError(
                    f"MCP tools {names[name]} and {definition.name} normalize to the "
                    f"same registry name {name}"
                )
            names[name] = definition.name
            try:
                input_schema = _validate_object_schema(
                    definition.input_schema or {"type": "object", "properties": {}},
                    f"tool {definition.name} input schema",
                )
                output_schema = (
                    _validate_object_schema(
                        definition.output_schema, f"tool {definition.name} output schema"
                    )
                    if definition.output_schema
                    else {}
                )
            except (ValueError, TypeError) as exc:
                raise ToolDefinitionError(
                    f"MCP tool {definition.name} has an invalid schema: {exc}"
                ) from exc
            prepared.append(
                (
                    name,
                    definition.model_copy(
                        update={
                            "input_schema": input_schema,
                            "output_schema": output_schema,
                        }
                    ),
                )
            )
        return prepared

    async def _conflicts(
        self, server: MCPServer, prepared: list[tuple[str, MCPToolDefinition]]
    ) -> list[str]:
        rows = list((await self.db.scalars(select(Tool))).all())
        by_name = {tool.name: tool for tool in rows}
        synced_by_remote = {
            str((tool.config or {}).get("remote_name")): tool
            for tool in rows
            if tool.source_type == ToolSourceType.MCP.value
            and (tool.config or {}).get("server_id") == server.id
        }
        conflicts: list[str] = []
        for name, definition in prepared:
            occupant = by_name.get(name)
            target = synced_by_remote.get(definition.name)
            if occupant is not None and occupant is not target:
                conflicts.append(
                    f"tool name {name} already exists ({occupant.source_type})"
                )
        return conflicts

    async def _record_sync_error(self, server: MCPServer, message: str) -> None:
        server.last_error = (message or "MCP sync failed")[:1000]
        await self.db.commit()

    async def _load(self, server_id: str) -> MCPServer:
        server = await self.db.get(MCPServer, server_id)
        if server is None:
            raise NotFoundError(f"MCP server {server_id} not found")
        return server

    async def _get_by_name(self, name: str) -> MCPServer | None:
        return await self.db.scalar(select(MCPServer).where(MCPServer.name == name))

    async def _server_tools(self, server_id: str) -> list[Tool]:
        rows = await self.db.scalars(
            select(Tool).where(Tool.source_type == ToolSourceType.MCP.value)
        )
        return sorted(
            (
                tool
                for tool in rows.all()
                if (tool.config or {}).get("server_id") == server_id
            ),
            key=lambda tool: tool.name,
        )

    async def _latest_versions(self) -> dict[str, int]:
        rows = await self.db.scalars(select(ToolVersion))
        versions: dict[str, int] = {}
        for row in rows.all():
            if row.version > versions.get(row.tool_id, 0):
                versions[row.tool_id] = row.version
        return versions

    @staticmethod
    def _require_admin(actor: UserContext) -> None:
        if not actor.has("tools:admin"):
            raise PermissionDeniedError("Missing permission: tools:admin")

    @staticmethod
    def _tool_out(tool: Tool, current_version: int) -> ToolOut:
        return ToolService._out(tool, current_version)

    def _out(self, server: MCPServer, actor: UserContext) -> MCPServerOut:
        admin = actor.is_superuser or actor.has("tools:admin")
        env = dict(server.env or {})
        headers = dict(server.headers or {})
        if not admin:
            env = redact_secret_mapping(env)
            headers = redact_secret_mapping(headers)
        return MCPServerOut(
            id=server.id,
            name=server.name,
            transport=server.transport,
            command=server.command,
            args=list(server.args or []),
            env=env,
            url=server.url,
            headers=headers,
            enabled=server.enabled,
            last_sync_at=server.last_sync_at,
            last_error=server.last_error,
            created_by=server.created_by,
            created_at=server.created_at,
            updated_at=server.updated_at,
        )


__all__ = ["MCPService", "synced_tool_name"]
