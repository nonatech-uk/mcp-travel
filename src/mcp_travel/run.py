"""Shared runner for MCP servers — supports stdio and HTTP transport."""

import os

from fastmcp import FastMCP


def serve(mcp: FastMCP) -> None:
    """Run an MCP server using MCP_TRANSPORT env var (default: stdio)."""
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "streamable-http":
        host = os.environ.get("MCP_HOST", "0.0.0.0")
        port = int(os.environ.get("MCP_PORT", "8080"))
        mcp.run(transport="streamable-http", host=host, port=port)
    else:
        mcp.run(transport="stdio")
