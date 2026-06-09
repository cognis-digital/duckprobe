"""Deep tests for duckprobe's Soda-Core-style data-quality engine.

Covers: the DSL parser (incl. severity + filters), every check kind, filtered
partitions, cross-column row expressions, referential integrity, percentiles,
group-by, anomaly/change detection against a metric store, JUnit export, the
bundled suite+datasets, auto-profiling/suggest, and the CLI surface
(table+json+junit, non-zero exit on error-severity findings). No network.
"""

import io
import json
import os
import sys

# duckprobe/tests/ -> duckprobe/ -> build_out/  (so `import duckprobe` works
# and demos/02-deep/duckprobe resolves)
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from duckprobe import (  # noqa: E402
    TOOL_NAME,
    TOOL_VERSION,
    EvalContext,
    bundled_customers_csv,
    bundled_dataset_csv,
    bundled_report,
    bundled_suite,
    load_csv_text,
    load_metric_store,
    parse_checks,
    probe,
    profile_table,
    run_checks,
    suggest_checks,
)
from duckprobe import cli  # noqa: E402

DEMO = os.path.join(ROOT, "demos", "02-deep", "duckprobe")
ORDERS = os.path.join(DEMO, "orders.csv")
ORDERS_CHECKS = os.path.join(DEMO, "orders.checks")
CUSTOMERS = os.path.join(DEMO, "customers.csv")


def _check(text, csv_text=None, ctx=None):
    table = load_csv_text(csv_text if csv_text is not None else bundled_dataset_csv())
    return run_checks(table, parse_checks(text), ctx)[0]


# --------------------------------------------------------------------------- #
# Constants / packaging
# --------------------------------------------------------------------------- #
def test_constants():
    assert TOOL_NAME == "duckprobe"
    assert TOOL_VERSION.count(".") == 2


# --------------------------------------------------------------------------- #
# DSL parsing + severity + filter
# --------------------------------------------------------------------------- #
def test_parse_severity_default_and_explicit():
    checks = parse_checks("not_null id\nnull_percent age < 5 [warn]\n# comment\n")
    assert len(checks) == 2
    assert checks[0].severity == "error"
    assert checks[1].severity == "warn" and checks[1].kind == "null_percent"


def test_parse_filter_suffix():
    c = parse_checks("not_null email where region = EU")[0]
    assert c.kind == "not_null" and c.filter == ("region", "=", "EU")
    c2 = parse_checks("avg total < 100 where status = refunded [warn]")[0]
    assert c2.severity == "warn" and c2.filter == ("status", "=", "refunded")


def test_parse_all_kinds():
    suite = (
        "row_count > 0\nrow_count between 1 and 9\nnot_null a\nnull_percent a < 5\n"
        "completeness a >= 95\nunique a\nno_duplicates a, b\nduplicate_percent a < 1\n"
        "min a >= 0\nmax a <= 9\navg a between 0 and 9\nsum a > 0\nstddev a < 9\n"
        "median a <= 9\npercentile a p95 < 9\npercentile a p50 between 0 and 9\n"
        "accepted_values s in x, y\ninvalid_values s in z\nmatches_regex a ^x$\n"
        "invalid_regex a ^x$\nlength a between 1 and 3\nin_range a between 0 and 9\n"
        "monotonic_increasing a\nfreshness d <= 7\nschema a, b\n"
        "row_expr a == b\nreference a in r.csv:id\ngroup_by g row_count >= 1\n"
        "group_by g avg a < 9\nanomaly row_count change < 30%\n"
        "anomaly avg a change < 10%\n"
    )
    kinds = {c.kind for c in parse_checks(suite)}
    expected = {"row_count_cmp", "row_count_between", "not_null", "null_percent",
                "completeness", "unique", "duplicate_percent", "agg_cmp",
                "agg_between", "percentile_cmp", "percentile_between",
                "accepted_values", "invalid_values", "matches_regex",
                "length_between", "in_range", "monotonic", "freshness", "schema",
                "row_expr", "reference", "group_by_row_count", "group_by_agg",
                "anomaly"}
    assert expected.issubset(kinds), expected - kinds


def test_parse_rejects_garbage():
    try:
        parse_checks("this is not a check")
        assert False, "expected ValueError"
    except ValueError:
        pass


# --------------------------------------------------------------------------- #
# Individual check semantics
# --------------------------------------------------------------------------- #
CSV = ("id,email,age,status\n"
       "1,a@x.com,10,ok\n"
       "2,b@x.com,20,ok\n"
       "2,,30,bad\n"
       "4,c@x.com,200,ok\n")


def test_not_null_and_null_percent():
    r = _check("not_null email", CSV)
    assert not r.passed and r.failing_rows == 1
    r = _check("null_percent email < 50", CSV)
    assert r.passed and r.observed == 25.0
    r = _check("completeness email >= 90", CSV)
    assert not r.passed


def test_unique_and_duplicate_percent():
    r = _check("unique id", CSV)
    assert not r.passed and r.observed == 1     # one dup
    r = _check("duplicate_percent id < 1", CSV)
    assert not r.passed and r.failing_rows == 1


def test_aggregates_and_median():
    assert _check("max age <= 130", CSV).passed is False
    assert _check("min age >= 0", CSV).passed is True
    avg = _check("avg age between 0 and 100", CSV)
    assert avg.passed and avg.observed == 65.0
    assert _check("sum age > 0", CSV).passed
    assert _check("stddev age < 1000", CSV).passed
    med = _check("median age between 0 and 100", CSV)
    assert med.passed and med.observed == 25.0   # median of 10,20,30,200


def test_percentile():
    # ages: 10,20,30,200 ; p100 = max = 200
    r = _check("percentile age p100 <= 200", CSV)
    assert r.passed and r.observed == 200.0
    r = _check("percentile age p0 >= 10", CSV)
    assert r.passed and r.observed == 10.0
    r = _check("percentile age p50 between 0 and 100", CSV)
    assert r.passed


def test_accepted_and_invalid_values():
    r = _check("accepted_values status in ok", CSV)
    assert not r.passed and r.observed == ["bad"]
    assert _check("invalid_values status in bad", CSV).failing_rows == 1


def test_regex_and_in_range_and_length():
    r = _check(r"matches_regex email ^[^@]+@[^@]+\.[^@]+$", CSV)
    assert r.passed                                  # null email is skipped
    r = _check("in_range age between 0 and 130", CSV)
    assert not r.passed and r.failing_rows == 1      # 200 out of range
    assert _check("length status between 1 and 3", CSV).passed


def test_schema_and_monotonic_and_freshness():
    assert _check("schema id, email, status", CSV).passed
    miss = _check("schema id, nope", CSV)
    assert not miss.passed and miss.observed == ["nope"]
    assert _check("monotonic_increasing id", CSV).passed   # 1,2,2,4 never decreases
    stale = _check("freshness d <= 1", "d\n2000-01-01\n")
    assert not stale.passed and stale.observed > 1


def test_missing_column_is_failure():
    r = _check("not_null does_not_exist", CSV)
    assert not r.passed and "not found" in r.detail


# --------------------------------------------------------------------------- #
# Filtered / partitioned checks
# --------------------------------------------------------------------------- #
def test_filter_partitions_rows():
    # only the two status=ok rows with non-null email -> not_null passes
    r = _check("not_null email where status = ok", CSV)
    assert r.passed and r.failing_rows == 0
    # the status=bad partition has the null email -> fails
    r = _check("not_null email where status = bad", CSV)
    assert not r.passed and r.failing_rows == 1
    # filtered aggregate
    r = _check("avg age <= 30 where status = ok", CSV)  # ages 10,20,200 -> avg 76.7
    assert not r.passed


def test_filter_numeric_op():
    r = _check("row_count == 1 where age > 100", CSV)   # only the age=200 row
    assert r.passed and r.observed == 1


# --------------------------------------------------------------------------- #
# Cross-column row expression
# --------------------------------------------------------------------------- #
def test_row_expr_passes_and_fails():
    data = ("q,p,t\n2,10,20\n3,5,15\n1,9,9\n")
    assert _check("row_expr t == q * p", data).passed
    bad = ("q,p,t\n2,10,20\n3,5,16\n")     # 3*5 != 16
    r = _check("row_expr t == q * p", bad)
    assert not r.passed and r.failing_rows == 1


def test_row_expr_rejects_unsafe():
    r = _check("row_expr __import__('os') == 1", "a\n1\n")
    assert not r.passed and "bad expression" in r.detail


def test_row_expr_skips_null_rows():
    data = ("q,p,t\n2,10,20\n,5,15\n")     # second row has null q -> skipped
    r = _check("row_expr t == q * p", data)
    assert r.passed and "0 of 1" in r.detail   # only 1 evaluable row, 0 fail


# --------------------------------------------------------------------------- #
# Referential integrity
# --------------------------------------------------------------------------- #
def test_reference_integrity(tmp_path):
    ref = tmp_path / "master.csv"
    ref.write_text("id\n1\n2\n3\n", encoding="utf-8")
    data = tmp_path / "facts.csv"
    data.write_text("fk\n1\n2\n9\n", encoding="utf-8")   # 9 is an orphan
    rep = probe(str(data), f"reference fk in master.csv:id\n", prefer_duckdb=False)
    r = rep.results[0]
    assert not r.passed and r.observed == 1 and "absent" in r.detail


def test_reference_all_valid(tmp_path):
    ref = tmp_path / "m.csv"
    ref.write_text("id\nA\nB\nC\n", encoding="utf-8")
    data = tmp_path / "f.csv"
    data.write_text("fk\nA\nB\n", encoding="utf-8")
    rep = probe(str(data), "reference fk in m.csv:id\n", prefer_duckdb=False)
    assert rep.results[0].passed


# --------------------------------------------------------------------------- #
# Group-by
# --------------------------------------------------------------------------- #
def test_group_by_row_count():
    data = ("g,v\nA,1\nA,2\nB,5\n")
    assert _check("group_by g row_count >= 1", data).passed
    r = _check("group_by g row_count >= 2", data)        # B has only 1
    assert not r.passed and r.failing_rows == 1


def test_group_by_agg():
    data = ("g,v\nA,1\nA,2\nB,50\n")
    r = _check("group_by g avg v < 10", data)            # B avg 50 fails
    assert not r.passed and r.failing_rows == 1
    assert _check("group_by g max v <= 100", data).passed


# --------------------------------------------------------------------------- #
# Anomaly / change detection + metric store
# --------------------------------------------------------------------------- #
def test_anomaly_baseline_then_change(tmp_path):
    hist = str(tmp_path / "hist.json")
    data = tmp_path / "d.csv"
    data.write_text("id,amount\n1,10\n2,20\n3,30\n", encoding="utf-8")
    suite = "anomaly row_count change < 50%\nanomaly avg amount change < 25%\n"
    rep1 = probe(str(data), suite, prefer_duckdb=False, metric_store=hist)
    assert rep1.passed                          # no baseline -> establish, pass
    stored = load_metric_store(hist)
    assert stored["row_count"] == 3.0 and stored["avg:amount"] == 20.0
    # mutate: avg jumps far beyond 25%
    data.write_text("id,amount\n1,100\n2,200\n3,300\n4,400\n", encoding="utf-8")
    rep2 = probe(str(data), suite, prefer_duckdb=False, metric_store=hist)
    avg_res = [r for r in rep2.results if "avg" in r.check][0]
    rc_res = [r for r in rep2.results if "row_count" in r.check][0]
    assert not avg_res.passed                   # 1150% change -> fail
    assert rc_res.passed                         # 33% within 50% -> pass
    assert not rep2.passed


# --------------------------------------------------------------------------- #
# JUnit export
# --------------------------------------------------------------------------- #
def test_junit_xml():
    rep = bundled_report()
    xml = rep.to_junit_xml()
    assert xml.startswith("<?xml")
    assert '<testsuite name="duckprobe"' in xml
    assert f'failures="{rep.errors}"' in xml
    assert "<failure " in xml          # at least one error-severity failure
    assert "<skipped " in xml          # the warn check renders as skipped


# --------------------------------------------------------------------------- #
# Bundled suite + datasets
# --------------------------------------------------------------------------- #
def test_bundled_dataset_loads():
    t = load_csv_text(bundled_dataset_csv())
    assert t.row_count == 15
    assert "customer_id" in t.columns and "email" in t.columns


def test_bundled_customers_master():
    t = load_csv_text(bundled_customers_csv())
    assert "id" in t.columns and t.row_count >= 10


def test_bundled_suite_parses_and_has_warn():
    checks = parse_checks(bundled_suite())
    assert len(checks) >= 12
    assert any(c.severity == "warn" for c in checks)
    assert any(c.kind == "group_by_row_count" for c in checks)
    assert any(c.filter is not None for c in checks)


def test_bundled_report_finds_injected_issues():
    rep = bundled_report()
    by_raw = {r.check.split(" [")[0]: r for r in rep.results}
    assert not by_raw["unique customer_id"].passed
    assert not by_raw["not_null email"].passed
    assert not by_raw[r"matches_regex email ^[^@\s]+@[^@\s]+\.[^@\s]+$"].passed
    assert not by_raw["accepted_values status in active, churned, trial"].passed
    assert not by_raw["in_range age between 0 and 120"].passed
    assert not by_raw["min balance >= 0"].passed
    # the orphan customer_id (9999) must be caught by referential integrity
    ref = [r for r in rep.results if r.kind == "reference"][0]
    assert not ref.passed and ref.observed >= 1
    assert rep.passed is False
    assert rep.errors >= 6
    comp = by_raw["completeness email >= 95"]
    assert comp.severity == "warn"


# --------------------------------------------------------------------------- #
# Profiling / suggest
# --------------------------------------------------------------------------- #
def test_profile_and_suggest():
    t = load_csv_text(bundled_dataset_csv())
    prof = profile_table(t)
    assert prof["row_count"] == 15
    assert prof["columns"]["age"]["type"] == "numeric"
    assert prof["columns"]["email"]["nulls"] == 1
    suite = suggest_checks(t)
    parsed = parse_checks(suite)
    assert any(c.kind == "schema" for c in parsed)


# --------------------------------------------------------------------------- #
# Demo files end-to-end
# --------------------------------------------------------------------------- #
def test_demo_orders_suite():
    with open(ORDERS_CHECKS, encoding="utf-8") as fh:
        rep = probe(ORDERS, fh.read(), prefer_duckdb=False)
    assert rep.row_count == 15
    failing = {r.kind for r in rep.results if not r.passed}
    assert "unique" in failing                # dup order_id 50001
    assert "matches_regex" in failing         # bad-email
    assert "accepted_values" in failing       # status=frozen
    assert "in_range" in failing              # quantity=-1
    assert "agg_cmp" in failing               # min total >= 0 fails (-8)
    assert "reference" in failing             # bad-email absent from customers
    # the cross-column invariant holds in the demo data -> row_expr passes
    rexpr = [r for r in rep.results if r.kind == "row_expr"][0]
    assert rexpr.passed
    # filtered EU min-total check passes (no negative totals in EU)
    eu = [r for r in rep.results if "where region = EU" in r.check]
    assert eu and eu[0].passed
    assert rep.passed is False


def test_demo_customers_master_present():
    t = load_csv_text(open(CUSTOMERS, encoding="utf-8").read())
    # the master keys orders on customer_email values (column 'customer_id')
    assert "customer_id" in t.columns and t.row_count >= 10


def test_clean_csv_passes(tmp_path):
    p = tmp_path / "clean.csv"
    p.write_text("id,email\n1,a@x.com\n2,b@x.com\n", encoding="utf-8")
    rep = probe(str(p), "not_null id\nunique id\nnot_null email\n", prefer_duckdb=False)
    assert rep.passed and rep.errors == 0


# --------------------------------------------------------------------------- #
# CLI surface
# --------------------------------------------------------------------------- #
def _run_cli(argv):
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        rc = cli.main(argv)
    finally:
        sys.stdout = old
    return rc, out.getvalue()


def test_cli_scan_json_nonzero_exit():
    rc, text = _run_cli(["--format", "json", "scan", "--no-duckdb"])
    assert rc == 1, "bundled dataset has error-severity failures -> exit 1"
    payload = json.loads(text)
    assert payload["tool"] == "duckprobe"
    assert payload["row_count"] == 15
    assert payload["errors"] >= 6
    assert payload["warnings"] >= 0
    assert "metrics" in payload
    assert all("severity" in r and "outcome" in r for r in payload["results"])


def test_cli_scan_junit():
    rc, text = _run_cli(["--format", "junit", "scan", "--no-duckdb"])
    assert rc == 1
    assert text.startswith("<?xml")
    assert "<testsuite" in text and "<failure " in text


def test_cli_check_table_and_exit():
    rc, text = _run_cli(["check", ORDERS, "--checks", ORDERS_CHECKS, "--no-duckdb"])
    assert rc == 1
    assert "duckprobe FAIL" in text
    assert "[FAIL]" in text


def test_cli_check_clean_exit_zero(tmp_path):
    p = tmp_path / "ok.csv"
    p.write_text("id\n1\n2\n3\n", encoding="utf-8")
    rc, _ = _run_cli(["check", str(p), "-c", "row_count > 0", "-c", "unique id",
                      "--no-duckdb"])
    assert rc == 0


def test_cli_warn_only_does_not_fail(tmp_path):
    p = tmp_path / "w.csv"
    p.write_text("id,email\n1,a@x.com\n2,\n", encoding="utf-8")
    rc, text = _run_cli(["check", str(p), "-c", "completeness email >= 99 [warn]",
                         "--no-duckdb"])
    assert rc == 0
    assert "[warn]" in text


def test_cli_metric_store_roundtrip(tmp_path):
    p = tmp_path / "m.csv"
    p.write_text("id,v\n1,10\n2,20\n", encoding="utf-8")
    hist = str(tmp_path / "h.json")
    rc, _ = _run_cli(["check", str(p), "-c", "anomaly avg v change < 10%",
                      "--metric-store", hist, "--no-duckdb"])
    assert rc == 0                       # first run establishes baseline
    assert os.path.exists(hist)
    stored = load_metric_store(hist)
    assert stored["avg:v"] == 15.0


def test_cli_rules_and_profile_and_suggest():
    rc, text = _run_cli(["--format", "json", "rules"])
    assert rc == 0 and len(json.loads(text)) >= 12
    rc, text = _run_cli(["--format", "json", "profile", ORDERS, "--no-duckdb"])
    assert rc == 0 and json.loads(text)["row_count"] == 15
    rc, text = _run_cli(["suggest", ORDERS, "--no-duckdb"])
    assert rc == 0 and "row_count > 0" in text


def test_cli_no_checks_is_usage_error(tmp_path):
    p = tmp_path / "x.csv"
    p.write_text("id\n1\n", encoding="utf-8")
    rc, _ = _run_cli(["check", str(p), "--no-duckdb"])
    assert rc == 2


if __name__ == "__main__":
    import inspect
    import tempfile
    import pathlib
    import traceback

    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            if "tmp_path" in inspect.signature(fn).parameters:
                with tempfile.TemporaryDirectory() as d:
                    fn(pathlib.Path(d))
            else:
                fn()
            print(f"ok   {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    sys.exit(1 if failed else 0)
