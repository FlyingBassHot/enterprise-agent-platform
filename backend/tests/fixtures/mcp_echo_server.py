"""Deterministic stdio MCP server used by the MCP client tests.

Run with the current interpreter:

    python tests/fixtures/mcp_echo_server.py

``MCP_ECHO_VARIANT`` changes the advertised tool set:
- ``v2``: ``echo`` gains an optional argument (schema/version bump test).
- ``v3``: ``add`` disappears (missing-tool disable test).
"""

from __future__ import annotations

import os

from mcp.server.mcpserver import MCPServer

VARIANT = os.environ.get("MCP_ECHO_VARIANT", "v1")

server = MCPServer("echo-tools")


if VARIANT == "v2":

    @server.tool()
    def echo(text: str, uppercase: bool = False) -> str:
        """Echo text, optionally upper-cased."""
        return text.upper() if uppercase else text

else:

    @server.tool()
    def echo(text: str) -> str:
        """Echo text back to the caller."""
        return text


if VARIANT != "v3":

    @server.tool()
    def add(a: int, b: int) -> dict:
        """Add two integers."""
        return {"sum": a + b}


if __name__ == "__main__":
    server.run("stdio")
