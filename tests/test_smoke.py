"""Smoke tests for DUCKPROBE. No network; stdlib only."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from duckprobe import TOOL_NAME, TOOL_VERSION
from duckprobe.core import load_table, parse_checks, run_checks, ProbeReport
from duckprobe.cli import main


GOOD_CSV = (
    "id,email,status,age\n"
    "1,a@x.com,active,30\n"
    "2,b@x.com,trial,40\n"
    "3,c@x.com,active,50\n"
)

BAD_CSV = (
    "id,email,status,age\n"
    "1,a@x.com,active,30\n"
    "1,bad,zombie,-5\n"      # dup id, bad email, bad status, negative age
    "3,,active,200\n"        # null email, age out of range
)


def _write(text: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
        fh.write(text)
    return path


class TestCore(unittest.TestCase):
    def test_metadata(self):
        self.assertEqual(TOOL_NAME, "duckprobe")
        self.assertEqual(TOOL_VERSION.count("."), 2)

    def test_load_stdlib(self):
        path = _write(GOOD_CSV)
        try:
            table, engine = load_table(path, prefer_duckdb=False)
            self.assertEqual(engine, "stdlib-csv")
            self.assertEqual(table.row_count, 3)
            self.assertEqual(table.columns, ["id", "email", "status", "age"])
            self.assertEqual(table.data["age"], [30, 40, 50])
        finally:
            os.unlink(path)

    def test_parse_all_check_kinds(self):
        text = "\n".join([
            "row_count > 0",
            "row_count between 1 and 9",
            "not_null id",
            "unique id",
            "no_duplicates id, status",
            "min age >= 0",
            "max age <= 130",
            "accepted_values status in active, trial, churned",
            "matches_regex email ^[^@]+@[^@]+$",
            "freshness ts <= 7",
        ])
        kinds = {c.kind for c in parse_checks(text)}
        self.assertEqual(kinds, {
            "row_count_cmp", "row_count_between", "not_null", "unique",
            "agg_cmp", "accepted_values", "matches_regex", "freshness",
        })

    def test_comments_ignored(self):
        checks = parse_checks("# header\nrow_count > 0   # inline\n\n")
        self.assertEqual(len(checks), 1)

    def test_all_pass(self):
        path = _write(GOOD_CSV)
        try:
            table, _ = load_table(path, prefer_duckdb=False)
            checks = parse_checks(
                "row_count > 0\nnot_null id\nunique id\n"
                "min age >= 0\naccepted_values status in active, trial"
            )
            self.assertTrue(all(r.passed for r in run_checks(table, checks)))
        finally:
            os.unlink(path)

    def test_failures_detected(self):
        path = _write(BAD_CSV)
        try:
            table, _ = load_table(path, prefer_duckdb=False)
            checks = parse_checks(
                "unique id\nnot_null email\nmin age >= 0\nmax age <= 130\n"
                "accepted_values status in active, trial, churned"
            )
            by = {r.check: r for r in run_checks(table, checks)}
            self.assertFalse(by["unique id"].passed)
            self.assertFalse(by["not_null email"].passed)
            self.assertFalse(by["min age >= 0"].passed)
            self.assertFalse(by["max age <= 130"].passed)
            self.assertFalse(
                by["accepted_values status in active, trial, churned"].passed)
        finally:
            os.unlink(path)

    def test_missing_column_fails_gracefully(self):
        path = _write(GOOD_CSV)
        try:
            table, _ = load_table(path, prefer_duckdb=False)
            r = run_checks(table, parse_checks("not_null nope"))[0]
            self.assertFalse(r.passed)
            self.assertIn("not found", r.detail)
        finally:
            os.unlink(path)

    def test_bad_check_raises(self):
        with self.assertRaises(ValueError):
            parse_checks("this is not a check")

    def test_report_dict_shape(self):
        path = _write(GOOD_CSV)
        try:
            table, engine = load_table(path, prefer_duckdb=False)
            results = run_checks(table, parse_checks("row_count > 0"))
            d = ProbeReport(path, engine, table.row_count, results).to_dict()
            self.assertTrue(d["passed"])
            self.assertEqual(d["checks_total"], 1)
            self.assertIn("results", d)
        finally:
            os.unlink(path)


class TestCli(unittest.TestCase):
    def test_check_pass_returns_zero(self):
        path = _write(GOOD_CSV)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                rc = main(["check", path, "-c", "row_count > 0",
                           "-c", "unique id", "--no-duckdb", "--format", "json"])
            self.assertEqual(rc, 0)
        finally:
            os.unlink(path)

    def test_check_fail_returns_one(self):
        path = _write(GOOD_CSV)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                rc = main(["check", path, "-c", "max age <= 10", "--no-duckdb"])
            self.assertEqual(rc, 1)
        finally:
            os.unlink(path)

    def test_no_checks_returns_two(self):
        path = _write(GOOD_CSV)
        try:
            rc = main(["check", path, "--no-duckdb"])
            self.assertEqual(rc, 2)
        finally:
            os.unlink(path)

    def test_auto_checks_subcommand(self):
        path = _write(GOOD_CSV)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                rc = main(["checks", path, "--no-duckdb", "--format", "json"])
            self.assertEqual(rc, 0)
        finally:
            os.unlink(path)

    def test_missing_file_returns_two(self):
        rc = main(["check",
                   os.path.join(tempfile.gettempdir(), "nope_xyz_123.csv"),
                   "-c", "row_count > 0", "--no-duckdb"])
        self.assertEqual(rc, 2)

    def test_json_output_is_valid(self):
        path = _write(GOOD_CSV)
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                main(["check", path, "-c", "row_count > 0",
                      "--no-duckdb", "--format", "json"])
            self.assertTrue(json.loads(buf.getvalue())["passed"])
        finally:
            os.unlink(path)

    def test_version_subprocess(self):
        out = subprocess.run(
            [sys.executable, "-m", "duckprobe", "--version"],
            capture_output=True, text=True,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        )
        self.assertEqual(out.returncode, 0)
        self.assertIn(TOOL_VERSION, out.stdout)


if __name__ == "__main__":
    unittest.main()
