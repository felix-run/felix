"""MCP protocol — inbound server + outbound client."""

from felix.mcp.client import tools_from_mcp_servers
from felix.mcp.server import handle_rpc

__all__ = ["handle_rpc", "tools_from_mcp_servers"]
