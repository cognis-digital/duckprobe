"""Hardening tests: error paths, edge cases, and input validation."""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
import warnings

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from duckprobe.core import (
    load_csv_text,
    save_metric_store,
    bundled_report,
)
from duckprobe.cli import main


class TestDuplicateColumnNames(unittest.TestCase):

    def test_dedup_produces_unique_names(self):
        t = load_csv_text("id,id,name\n1,2,alice\n3,4,bob\n")
        self.assertIn("id", t.data)
        self.assertIn("id_2", t.data)
        self.assertEqual(len(t.data["id"]), 2)
        self.assertEqual(len(t.data["id_2"]), 2)

    def test_three_identical_columns(self):
        t = load_csv_text("id,id,id\n1,2,3\n")
        self.assertIn("id", t.data)
        self.assertIn("id_2", t.data)
        self.assertIn("id_3", t.data)

    def test_no_data_loss(self):
        t = load_csv_text("x,x\n10,20\n30,40\n")
        self.assertEqual(t.data["x"], [10, 30])
        self.assertEqual(t.data["x_2"], [20, 40])


class TestSaveMetricStoreRobust(unittest.TestCase):

    def test_bad_dir_warns_not_raises(self):
        bad_path = os.path.join(
            tempfile.gettempdir(), "nonexistent_duckprobe_subdir_xyz", "m.json"
        )
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            save_metric_store(bad_path, {"row_count": 3.0})
        self.assertTrue(
            any(issubclass(w.category, RuntimeWarning) for w in caught),
            "Expected a RuntimeWarning for unwriteable path",
        )

    def test_valid_path_writes_file(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                save_metric_store(path, {"row_count": 5.0})
            self.assertTrue(os.path.exists(path))
            self.assertFalse(
                any(issubclass(w.category, RuntimeWarning) for w in caught),
                "No warning expected for a valid path",
            )
        finally:
            os.unlink(path)


class TestBundledReportTempCleanup(unittest.TestCase):

    def test_temp_dir_cleaned_up(self):
        import glob
        tmp_root = tempfile.gettempdir()
        before = set(glob.glob(os.path.join(tmp_root, "duckprobe_*")))
        bundled_report()
        after = set(glob.glob(os.path.join(tmp_root, "duckprobe_*")))
        new_dirs = after - before
        self.assertEqual(new_dirs, set(), f"Temp dir(s) not cleaned up: {new_dirs}")


class TestMcpServerImport(unittest.TestCase):

    def test_imports_without_error(self):
        try:
            from duckprobe import mcp_server  # noqa: F401
        except ImportError as e:
            self.fail(f"mcp_server import raised ImportError: {e}")


class TestCliEdgeCases(unittest.TestCase):

    def _run(self, argv):
        buf = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            rc = main(argv)
        return rc, buf.getvalue(), err.getvalue()

    def test_missing_data_file_returns_2(self):
        rc, _, err = self._run([
            "check",
            os.path.join(tempfile.gettempdir(), "no_such_file_xyz.csv"),
            "-c", "row_count > 0",
            "--no-duckdb",
        ])
        self.assertEqual(rc, 2)
        self.assertIn("error:", err.lower())

    def test_non_utf8_file_returns_2(self):
        fd, path = tempfile.mkstemp(suffix=".csv")
        with os.fdopen(fd, "wb") as fh:
            fh.write(b"id,name\n1,caf\xe9\n")
        try:
            rc, _, err = self._run(
                ["check", path, "-c", "row_count > 0", "--no-duckdb"]
            )
            self.assertEqual(rc, 2)
            self.assertIn("error:", err.lower())
        finally:
            os.unlink(path)

    def test_malformed_check_returns_2(self):
        fd, path = tempfile.mkstemp(suffix=".csv")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("id\n1\n")
        try:
            rc, _, err = self._run([
                "check", path, "-c", "not a valid check at all", "--no-duckdb"
            ])
            self.assertEqual(rc, 2)
            self.assertIn("error:", err.lower())
        finally:
            os.unlink(path)

    def test_header_only_csv_row_count_check(self):
        import json
        fd, path = tempfile.mkstemp(suffix=".csv")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("id,name\n")
        try:
            rc, out, _ = self._run([
                "check", path, "-c", "row_count > 0", "--no-duckdb", "--format", "json"
            ])
            data = json.loads(out)
            self.assertEqual(rc, 1)
            self.assertEqual(data["row_count"], 0)
        finally:
            os.unlink(path)


class TestWebhookUrlValidation(unittest.TestCase):

    def _invoke(self, url, stdin_data="{}"):
        import subprocess
        webhook_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "integrations", "webhook.py"
        )
        result = subprocess.run(
            [sys.executable, webhook_path, "--url", url],
            input=stdin_data,
            capture_output=True,
            text=True,
        )
        return result.returncode, result.stderr

    def test_file_scheme_rejected(self):
        rc, err = self._invoke("file:///etc/passwd")
        self.assertEqual(rc, 2)
        self.assertIn("http", err.lower())

    def test_ftp_scheme_rejected(self):
        rc, err = self._invoke("ftp://example.com/path")
        self.assertEqual(rc, 2)

    def test_empty_stdin_rejected(self):
        rc, err = self._invoke("https://example.com/hook", stdin_data="")
        self.assertEqual(rc, 2)
        self.assertIn("error:", err.lower())


if __name__ == "__main__":
    unittest.main()
