from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import require_permission
from app.core.contracts import UserContext
from app.infrastructure.database.session import get_db
from app.modules.tool.mcp_service import MCPService
from app.modules.tool.schemas import (
    MCPServerCreate,
    MCPServerOut,
    MCPServerUpdate,
    MCPSyncResult,
    ToolCreate,
    ToolOut,
    ToolTestRequest,
    ToolTestResult,
    ToolUpdate,
)
from app.modules.tool.service import ToolService

router = APIRouter(prefix="/tools", tags=["tools"])
mcp_router = APIRouter(prefix="/mcp-servers", tags=["mcp"])


@router.get("", response_model=list[ToolOut])
async def list_tools(
    user: UserContext = Depends(require_permission("tools:read")),
    db: AsyncSession = Depends(get_db),
):
    return await ToolService(db).list_tools(user)


@router.post("", response_model=ToolOut, status_code=201)
async def create_tool(
    payload: ToolCreate,
    user: UserContext = Depends(require_permission("tools:admin")),
    db: AsyncSession = Depends(get_db),
):
    return await ToolService(db).create_tool(payload, user)


@router.get("/{tool_id}", response_model=ToolOut)
async def get_tool(
    tool_id: str,
    user: UserContext = Depends(require_permission("tools:read")),
    db: AsyncSession = Depends(get_db),
):
    return await ToolService(db).get_tool(tool_id, user)


@router.put("/{tool_id}", response_model=ToolOut)
async def update_tool(
    tool_id: str,
    payload: ToolUpdate,
    user: UserContext = Depends(require_permission("tools:admin")),
    db: AsyncSession = Depends(get_db),
):
    return await ToolService(db).update_tool(tool_id, payload, user)


@router.delete("/{tool_id}", status_code=204)
async def delete_tool(
    tool_id: str,
    user: UserContext = Depends(require_permission("tools:admin")),
    db: AsyncSession = Depends(get_db),
):
    await ToolService(db).delete_tool(tool_id, user)


@router.post("/{tool_id}/test", response_model=ToolTestResult)
async def test_tool(
    tool_id: str,
    payload: ToolTestRequest,
    user: UserContext = Depends(require_permission("tools:write")),
    db: AsyncSession = Depends(get_db),
):
    return await ToolService(db).test_tool(tool_id, payload.arguments, user)


@router.post("/{tool_id}/enable", response_model=ToolOut)
async def enable_tool(
    tool_id: str,
    user: UserContext = Depends(require_permission("tools:admin")),
    db: AsyncSession = Depends(get_db),
):
    return await ToolService(db).set_enabled(tool_id, True, user)


@router.post("/{tool_id}/disable", response_model=ToolOut)
async def disable_tool(
    tool_id: str,
    user: UserContext = Depends(require_permission("tools:admin")),
    db: AsyncSession = Depends(get_db),
):
    return await ToolService(db).set_enabled(tool_id, False, user)


# ---------------------------------------------------------------------------
# MCP servers (admin only)
# ---------------------------------------------------------------------------


@mcp_router.get("", response_model=list[MCPServerOut])
async def list_mcp_servers(
    user: UserContext = Depends(require_permission("tools:admin")),
    db: AsyncSession = Depends(get_db),
):
    return await MCPService(db).list_servers(user)


@mcp_router.post("", response_model=MCPServerOut, status_code=201)
async def create_mcp_server(
    payload: MCPServerCreate,
    user: UserContext = Depends(require_permission("tools:admin")),
    db: AsyncSession = Depends(get_db),
):
    return await MCPService(db).create_server(payload, user)


@mcp_router.get("/{server_id}", response_model=MCPServerOut)
async def get_mcp_server(
    server_id: str,
    user: UserContext = Depends(require_permission("tools:admin")),
    db: AsyncSession = Depends(get_db),
):
    return await MCPService(db).get_server(server_id, user)


@mcp_router.patch("/{server_id}", response_model=MCPServerOut)
async def update_mcp_server(
    server_id: str,
    payload: MCPServerUpdate,
    user: UserContext = Depends(require_permission("tools:admin")),
    db: AsyncSession = Depends(get_db),
):
    return await MCPService(db).update_server(server_id, payload, user)


@mcp_router.delete("/{server_id}", status_code=204)
async def delete_mcp_server(
    server_id: str,
    user: UserContext = Depends(require_permission("tools:admin")),
    db: AsyncSession = Depends(get_db),
):
    await MCPService(db).delete_server(server_id, user)


@mcp_router.post("/{server_id}/sync", response_model=MCPSyncResult)
async def sync_mcp_server(
    server_id: str,
    user: UserContext = Depends(require_permission("tools:admin")),
    db: AsyncSession = Depends(get_db),
):
    return await MCPService(db).sync(server_id, user)


@mcp_router.get("/{server_id}/tools", response_model=list[ToolOut])
async def list_mcp_server_tools(
    server_id: str,
    user: UserContext = Depends(require_permission("tools:admin")),
    db: AsyncSession = Depends(get_db),
):
    return await MCPService(db).list_server_tools(server_id, user)
