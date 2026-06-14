"""DUCKPROBE MCP server — exposes duckprobe as an MCP tool for Cognis.Studio."""
from __future__ import annotations
import json
from duckprobe.core import bundled_report, probe

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
    def duckprobe_scan(target: str, checks: str = "") -> str:
        """Run data-quality checks on a file via duckprobe. Returns JSON findings.

        Args:
            target: Path to a CSV or DuckDB-readable data file.
            checks: DSL check text (one check per line).
            If empty, runs the bundled suite.
        """
        try:
            if checks.strip():
                report = probe(target, checks, prefer_duckdb=True)
            else:
                report = bundled_report(prefer_duckdb=True)
            return json.dumps(report.to_dict(), indent=2, default=str)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"error": str(exc), "passed": False})

    app.run()
    return 0
