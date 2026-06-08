"""DUCKPROBE command-line interface.

    duckprobe check DATA --checks CHECKS_FILE [--format table|json] [--no-duckdb]
    duckprobe check DATA -c "row_count > 0" -c "not_null id"
    duckprobe checks DATA [--format ...]        # quick auto-profile of a file

Exit codes: 0 = all checks passed, 1 = one or more failed, 2 = usage/IO error.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from . import TOOL_NAME, TOOL_VERSION
from .core import load_table, parse_checks, run_checks, ProbeReport


def _build_report(data_path: str, check_text: str, prefer_duckdb: bool) -> ProbeReport:
    table, engine = load_table(data_path, prefer_duckdb=prefer_duckdb)
    checks = parse_checks(check_text)
    results = run_checks(table, checks)
    return ProbeReport(source=data_path, engine=engine,
                       row_count=table.row_count, results=results)


def _auto_checks(data_path: str, prefer_duckdb: bool) -> str:
    """Generate a sensible default check set by profiling the file."""
    table, _ = load_table(data_path, prefer_duckdb=prefer_duckdb)
    lines = ["row_count > 0"]
    for col in table.columns:
        vals = table.data[col]
        non_null = [v for v in vals if v is not None]
        if not non_null:
            continue
        null_ratio = 1 - (len(non_null) / max(len(vals), 1))
        if null_ratio == 0:
            lines.append(f"not_null {col}")
        distinct = len({str(v) for v in non_null})
        if distinct == len(non_null) and len(non_null) > 1:
            lines.append(f"unique {col}")
    return "\n".join(lines)


def _render_table(report: ProbeReport) -> str:
    out: List[str] = []
    status = "PASS" if report.passed else "FAIL"
    out.append(f"DUCKPROBE {status}  source={report.source}  "
               f"engine={report.engine}  rows={report.row_count}")
    out.append("-" * 72)
    for r in report.results:
        mark = "ok  " if r.passed else "FAIL"
        line = f"[{mark}] {r.check}"
        if r.observed is not None or r.detail:
            extra = f"observed={r.observed}"
            if r.detail and not r.passed:
                extra += f"  ({r.detail})"
            line += f"   {extra}"
        out.append(line)
    out.append("-" * 72)
    out.append(f"{report.failed_count} failed / {len(report.results)} checks")
    return "\n".join(out)


def _emit(report: ProbeReport, fmt: str) -> None:
    if fmt == "json":
        print(json.dumps(report.to_dict(), indent=2, default=str))
    else:
        print(_render_table(report))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=TOOL_NAME,
        description="Zero-setup data-quality checks on any file via DuckDB.",
    )
    p.add_argument("--version", action="version",
                   version=f"{TOOL_NAME} {TOOL_VERSION}")
    sub = p.add_subparsers(dest="command", required=True)

    chk = sub.add_parser("check", help="run data-quality checks on a file")
    chk.add_argument("data", help="path to CSV / Parquet / JSON data file")
    chk.add_argument("--checks", help="path to a checks file (DSL, one per line)")
    chk.add_argument("-c", "--check", action="append", default=[],
                     dest="inline_checks", metavar="CHECK",
                     help="inline check (repeatable)")
    chk.add_argument("--format", choices=("table", "json"), default="table")
    chk.add_argument("--no-duckdb", action="store_true",
                     help="force the stdlib CSV engine even if duckdb is installed")

    pro = sub.add_parser("checks", help="auto-profile a file into a check set")
    pro.add_argument("data", help="path to data file")
    pro.add_argument("--format", choices=("table", "json"), default="table")
    pro.add_argument("--no-duckdb", action="store_true")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    prefer_duckdb = not getattr(args, "no_duckdb", False)

    try:
        if args.command == "checks":
            text = _auto_checks(args.data, prefer_duckdb)
            report = _build_report(args.data, text, prefer_duckdb)
            _emit(report, args.format)
            return 0 if report.passed else 1

        # command == "check"
        check_text = ""
        if args.checks:
            with open(args.checks, "r", encoding="utf-8") as fh:
                check_text = fh.read()
        if args.inline_checks:
            check_text += "\n" + "\n".join(args.inline_checks)
        if not check_text.strip():
            print("error: no checks provided (use --checks FILE or -c CHECK)",
                  file=sys.stderr)
            return 2

        report = _build_report(args.data, check_text, prefer_duckdb)
        _emit(report, args.format)
        return 0 if report.passed else 1

    except (OSError, ValueError, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
