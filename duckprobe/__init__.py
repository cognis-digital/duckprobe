"""DUCKPROBE — zero-setup, Soda-Core-style data-quality checks on any file.

Point it at a CSV (or any DuckDB-readable file when ``duckdb`` is installed)
plus a small set of human-readable checks, and get a pass / fail / warn report
with per-check severity. Beyond column metrics it does filtered/partitioned
checks, cross-column row expressions, referential integrity, percentiles,
group-by, and anomaly/change detection against a JSON metric store — the
features that make soda-core a real data-quality tool. Ships with a real
bundled suite + datasets so ``duckprobe scan`` is useful with zero
configuration. Pure standard library.
"""

from .core import (
    TOOL_NAME,
    TOOL_VERSION,
    SEVERITIES,
    Table,
    Check,
    CheckResult,
    ProbeReport,
    EvalContext,
    load_table,
    load_csv_text,
    parse_checks,
    run_checks,
    probe,
    profile_table,
    suggest_checks,
    load_metric_store,
    save_metric_store,
    bundled_dataset_csv,
    bundled_customers_csv,
    bundled_suite,
    bundled_report,
)

__all__ = [
    "TOOL_NAME",
    "TOOL_VERSION",
    "SEVERITIES",
    "Table",
    "Check",
    "CheckResult",
    "ProbeReport",
    "EvalContext",
    "load_table",
    "load_csv_text",
    "parse_checks",
    "run_checks",
    "probe",
    "profile_table",
    "suggest_checks",
    "load_metric_store",
    "save_metric_store",
    "bundled_dataset_csv",
    "bundled_customers_csv",
    "bundled_suite",
    "bundled_report",
]
