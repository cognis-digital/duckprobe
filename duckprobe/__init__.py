"""DUCKPROBE — zero-setup data-quality checks on any file or warehouse.

Spirit: Soda Core + DuckDB. Point it at a CSV/Parquet/JSON file (or any DuckDB
connection string) and a small set of human-readable checks, and get a pass/fail
report. Uses DuckDB when available for fast warehouse-grade SQL; falls back to a
pure-stdlib CSV engine so it runs with zero install.
"""

from .core import (
    Check,
    CheckResult,
    ProbeReport,
    parse_checks,
    run_checks,
    load_table,
)

TOOL_NAME = "duckprobe"
TOOL_VERSION = "0.1.0"

__all__ = [
    "Check",
    "CheckResult",
    "ProbeReport",
    "parse_checks",
    "run_checks",
    "load_table",
    "TOOL_NAME",
    "TOOL_VERSION",
]
