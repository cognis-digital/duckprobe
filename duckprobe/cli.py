"""DUCKPROBE command-line interface.

    duckprobe check DATA --checks SUITE [--format table|json|junit] [--no-duckdb]
    duckprobe check DATA -c "row_count > 0" -c "not_null id [warn]"
    duckprobe check DATA --checks SUITE --metric-store hist.json   # anomaly mode
    duckprobe scan                              # run the bundled suite+datasets
    duckprobe profile DATA                      # per-column profile
    duckprobe suggest DATA                      # auto-generate a starter suite
    duckprobe rules                             # list the bundled suite checks
    duckprobe --version

Exit codes: 0 = no error-severity failures, 1 = one or more error failures,
2 = usage / IO error.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from .core import (
    TOOL_NAME,
    TOOL_VERSION,
    ProbeReport,
    bundled_report,
    bundled_suite,
    load_table,
    parse_checks,
    probe,
    profile_table,
    suggest_checks,
)

_OUTCOME_MARK = {"pass": "ok  ", "fail": "FAIL", "warn": "warn"}


def _render_report(report: ProbeReport) -> str:
    out: List[str] = []
    status = "PASS" if report.passed else "FAIL"
    out.append(f"{TOOL_NAME} {status}  source={report.source}  "
               f"engine={report.engine}  rows={report.row_count}")
    out.append("-" * 76)
    for r in report.results:
        mark = _OUTCOME_MARK[r.outcome]
        line = f"[{mark}] {r.check}"
        extra = ""
        if r.observed is not None:
            extra = f"observed={r.observed}"
        if not r.passed and r.detail:
            extra = (extra + "  " if extra else "") + f"({r.detail})"
        if extra:
            line += f"   {extra}"
        out.append(line)
    out.append("-" * 76)
    out.append(f"{report.errors} error(s), {report.warnings} warning(s) "
               f"across {len(report.results)} checks")
    return "\n".join(out)


def _emit_report(report: ProbeReport, fmt: str) -> None:
    if fmt == "json":
        print(json.dumps(report.to_dict(), indent=2, default=str))
    elif fmt == "junit":
        print(report.to_junit_xml(), end="")
    else:
        print(_render_report(report))


def _emit_obj(obj, fmt: str, table_render) -> None:
    if fmt == "json":
        print(json.dumps(obj, indent=2, default=str))
    else:
        print(table_render(obj))


def _gather_checks(args) -> str:
    text = ""
    if getattr(args, "checks", None):
        with open(args.checks, "r", encoding="utf-8") as fh:
            text = fh.read()
    if getattr(args, "inline_checks", None):
        text += "\n" + "\n".join(args.inline_checks)
    return text


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=TOOL_NAME,
        description="Soda-Core-style data-quality checks on any file. Zero install.",
    )
    p.add_argument("--version", action="version",
                   version=f"{TOOL_NAME} {TOOL_VERSION}")
    p.add_argument("--format", choices=("table", "json", "junit"), default="table",
                   help="output format (default: table)")
    sub = p.add_subparsers(dest="command", required=True)

    chk = sub.add_parser("check", help="run data-quality checks on a file")
    chk.add_argument("data", help="path to CSV / Parquet / JSON data file")
    chk.add_argument("--checks", help="path to a checks file (DSL, one per line)")
    chk.add_argument("-c", "--check", action="append", default=[],
                     dest="inline_checks", metavar="CHECK",
                     help="inline check (repeatable)")
    chk.add_argument("--metric-store", metavar="JSON",
                     help="JSON metric-history file; enables anomaly checks and is "
                          "updated with this run's metrics")
    chk.add_argument("--no-duckdb", action="store_true",
                     help="force the stdlib CSV engine even if duckdb is installed")

    sc = sub.add_parser("scan", help="run the bundled suite against the bundled datasets")
    sc.add_argument("--no-duckdb", action="store_true")

    pr = sub.add_parser("profile", help="per-column profile of a file")
    pr.add_argument("data", help="path to data file")
    pr.add_argument("--no-duckdb", action="store_true")

    sg = sub.add_parser("suggest", help="auto-generate a starter check suite")
    sg.add_argument("data", help="path to data file")
    sg.add_argument("--no-duckdb", action="store_true")

    sub.add_parser("rules", help="list the bundled suite checks")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    fmt = args.format
    prefer_duckdb = not getattr(args, "no_duckdb", False)

    try:
        if args.command == "scan":
            report = bundled_report(prefer_duckdb=prefer_duckdb)
            _emit_report(report, fmt)
            return 0 if report.passed else 1

        if args.command == "rules":
            checks = parse_checks(bundled_suite())
            if fmt == "json":
                print(json.dumps(
                    [{"check": c.raw, "kind": c.kind, "severity": c.severity}
                     for c in checks], indent=2))
            else:
                for c in checks:
                    print(f"[{c.severity:5}] {c.raw}")
            return 0

        if args.command == "profile":
            table, _ = load_table(args.data, prefer_duckdb=prefer_duckdb)
            prof = profile_table(table)

            def render(p):
                lines = [f"profile  rows={p['row_count']}", "-" * 76,
                         f"{'column':<18}{'type':<9}{'nulls':>7}{'null%':>9}{'distinct':>10}"]
                for name, info in p["columns"].items():
                    lines.append(f"{name:<18}{info['type']:<9}{info['nulls']:>7}"
                                 f"{info['null_percent']:>9}{info['distinct']:>10}")
                return "\n".join(lines)

            _emit_obj(prof, fmt, render)
            return 0

        if args.command == "suggest":
            table, _ = load_table(args.data, prefer_duckdb=prefer_duckdb)
            suite = suggest_checks(table)
            if fmt == "json":
                print(json.dumps({"suite": suite.splitlines()}, indent=2))
            else:
                print(suite)
            return 0

        # command == "check"
        check_text = _gather_checks(args)
        if not check_text.strip():
            print("error: no checks provided (use --checks FILE or -c CHECK)",
                  file=sys.stderr)
            return 2
        report = probe(args.data, check_text, prefer_duckdb=prefer_duckdb,
                       metric_store=getattr(args, "metric_store", None))
        _emit_report(report, fmt)
        return 0 if report.passed else 1

    except (OSError, ValueError, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
