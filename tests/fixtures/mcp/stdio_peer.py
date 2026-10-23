"""An MCP server over stdio, built on the official SDK, for the outbound client's tests.

Run as `python stdio_peer.py`: Felix's stdio transport spawns it under the stdio allowlist with
a scrubbed environment, so it imports nothing but the SDK.
"""

from mcp.server.mcpserver import MCPServer

server = MCPServer("stdio-peer", instructions="Use echo to repeat text back.")


@server.tool()
def echo(text: str) -> str:
    """Repeat the text back."""
    return f"echo:{text}"


@server.tool()
def fail() -> str:
    """Always fails."""
    raise ValueError("stdio peer refused")


if __name__ == "__main__":
    server.run("stdio")
