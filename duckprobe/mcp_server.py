"""DUCKPROBE MCP server — exposes scan() as an MCP tool for Cognis.Studio."""
from __future__ import annotations
from duckprobe.core import scan, to_json

def serve() -> int:
    """Start an MCP stdio server. Requires the optional 'mcp' extra:
        pip install "cognis-duckprobe[mcp]"
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except Exception:
        print("Install the MCP extra: pip install 'cognis-duckprobe[mcp]'")
        return 1
    app = FastMCP("duckprobe")

    @app.tool()
    def duckprobe_scan(target: str) -> str:
        """Zero-setup data-quality checks on any file or warehouse via DuckDB. Returns JSON findings."""
        return to_json(scan(target))

    app.run()
    return 0
