"""DUCKPROBE engine — a Soda-Core-style data-quality check engine.

A *table* is a column-oriented in-memory view of a CSV (or DuckDB-readable)
file. Checks are written in a small, human-readable DSL (one per line) and are
evaluated against the table, producing pass/fail results with a **severity**
(``error`` blocks, ``warn`` reports). This mirrors how soda-core distinguishes
``fail`` thresholds from ``warn`` thresholds.

The tool is pure standard library and ships with a real, bundled data-quality
suite + datasets (see ``bundled_suite``/``bundled_dataset_csv``) so it is useful
the moment it is installed — ``duckprobe scan`` runs that suite end to end.

Beyond simple column metrics it implements the features that make soda-core a
real tool:

  * **Filtered / partitioned checks** — ``... where region = NA`` evaluates a
    check only over the matching subset (SodaCL ``filter``).
  * **Cross-column row expressions** — ``row_expr total == quantity *
    unit_price`` validates an arithmetic/comparison invariant per row.
  * **Referential integrity** — ``reference customer_id in customers.csv:id``
    asserts every value exists in another file's column (SodaCL ``reference``).
  * **Percentiles & group-by** — ``percentile latency_ms p95 < 500`` and
    ``group_by region row_count >= 1`` (every group must satisfy the test).
  * **Anomaly / change detection** — ``anomaly row_count change < 30%`` compares
    a metric to the previous run stored in a JSON **metric store**
    (soda-core's signature capability), with no external service.
  * **CI exports** — JSON *and* JUnit XML for pipeline gating.

Check DSL (``#`` comments allowed, optional trailing ``[error|warn]`` severity,
optional trailing ``where COL OP VALUE`` filter on most column checks)::

    row_count > 0
    row_count between 1 and 100000
    not_null email
    null_percent age < 5
    completeness email >= 99
    unique customer_id
    no_duplicates email, region
    duplicate_percent email < 1
    min age >= 0
    max age <= 130
    avg score between 0 and 100
    sum amount > 0
    stddev latency_ms < 500
    percentile latency_ms p95 < 500
    median price between 1 and 1000
    accepted_values status in active, churned, trial
    invalid_values status in 0, -1
    matches_regex email ^[^@]+@[^@]+\\.[^@]+$
    invalid_regex phone ^\\+?[0-9 .-]{7,}$
    length name between 1 and 64
    in_range age between 0 and 130
    monotonic_increasing seq
    freshness updated_at <= 7
    schema id, email, status
    row_expr total == quantity * unit_price
    reference customer_id in customers.csv:id
    group_by region row_count >= 1
    anomaly row_count change < 30%
    # filters work on most checks:
    not_null email where region = EU
    avg total < 100 where status = refunded
"""

from __future__ import annotations

import csv
import io
import math
import os
import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple
from xml.sax.saxutils import escape as _xml_escape

TOOL_NAME = "duckprobe"
TOOL_VERSION = "0.3.0"

SEVERITIES = ("error", "warn")


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class Table:
    columns: List[str]
    data: Dict[str, List[Any]]          # column name -> python values (None = missing)
    row_count: int

    def col(self, name: str) -> List[Any]:
        if name not in self.data:
            raise KeyError(name)
        return self.data[name]

    def filtered(self, predicate: Callable[[int], bool]) -> "Table":
        idx = [i for i in range(self.row_count) if predicate(i)]
        data = {c: [self.data[c][i] for i in idx] for c in self.columns}
        return Table(columns=list(self.columns), data=data, row_count=len(idx))


@dataclass
class Check:
    raw: str
    kind: str
    severity: str = "error"
    args: Dict[str, Any] = field(default_factory=dict)
    filter: Optional[Tuple[str, str, str]] = None   # (column, op, literal)


@dataclass
class CheckResult:
    check: str
    kind: str
    severity: str
    passed: bool
    observed: Any
    failing_rows: int = 0
    detail: str = ""

    @property
    def outcome(self) -> str:
        if self.passed:
            return "pass"
        return "fail" if self.severity == "error" else "warn"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "check": self.check,
            "kind": self.kind,
            "severity": self.severity,
            "passed": self.passed,
            "outcome": self.outcome,
            "observed": self.observed,
            "failing_rows": self.failing_rows,
            "detail": self.detail,
        }


@dataclass
class ProbeReport:
    source: str
    engine: str
    row_count: int
    results: List[CheckResult]
    metrics: Dict[str, Any] = field(default_factory=dict)

    @property
    def errors(self) -> int:
        return sum(1 for r in self.results if not r.passed and r.severity == "error")

    @property
    def warnings(self) -> int:
        return sum(1 for r in self.results if not r.passed and r.severity == "warn")

    @property
    def passed(self) -> bool:
        """The dataset passes only when no *error*-severity check failed."""
        return self.errors == 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool": TOOL_NAME,
            "version": TOOL_VERSION,
            "source": self.source,
            "engine": self.engine,
            "row_count": self.row_count,
            "checks_total": len(self.results),
            "errors": self.errors,
            "warnings": self.warnings,
            "passed": self.passed,
            "metrics": self.metrics,
            "results": [r.to_dict() for r in self.results],
        }

    def to_junit_xml(self) -> str:
        """Render a JUnit XML <testsuite> for CI consumption (soda CI gating)."""
        failures = self.errors
        cases = []
        for r in self.results:
            name = _xml_escape(r.check)
            if r.passed:
                cases.append(f'    <testcase classname="{TOOL_NAME}" name="{name}"/>')
            elif r.severity == "warn":
                cases.append(
                    f'    <testcase classname="{TOOL_NAME}" name="{name}">\n'
                    f'      <skipped message="{_xml_escape(r.detail)}"/>\n'
                    f'    </testcase>'
                )
            else:
                cases.append(
                    f'    <testcase classname="{TOOL_NAME}" name="{name}">\n'
                    f'      <failure message="{_xml_escape(r.detail)}">'
                    f'observed={_xml_escape(str(r.observed))}</failure>\n'
                    f'    </testcase>'
                )
        body = "\n".join(cases)
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<testsuite name="{TOOL_NAME}" tests="{len(self.results)}" '
            f'failures="{failures}" skipped="{self.warnings}">\n'
            f'{body}\n'
            '</testsuite>\n'
        )


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def _coerce(value: Optional[str]) -> Any:
    """Sniff a raw string into int / float / bool / None / str."""
    if value is None:
        return None
    s = value.strip()
    if s == "" or s.lower() in ("null", "na", "n/a", "none", "nan"):
        return None
    low = s.lower()
    if low in ("true", "false"):
        return low == "true"
    if re.fullmatch(r"[+-]?\d+", s):
        try:
            return int(s)
        except ValueError:
            pass
    try:
        f = float(s)
        if not math.isnan(f) and not math.isinf(f):
            return f
    except ValueError:
        pass
    return s


def _load_with_duckdb(path: str) -> Optional[Table]:
    try:
        import duckdb  # type: ignore
    except Exception:
        return None
    con = duckdb.connect(database=":memory:")
    try:
        rel = con.sql(f"SELECT * FROM '{path}'")
        cols = list(rel.columns)
        rows = rel.fetchall()
    finally:
        con.close()
    data: Dict[str, List[Any]] = {c: [] for c in cols}
    for row in rows:
        for c, v in zip(cols, row):
            data[c].append(v)
    return Table(columns=cols, data=data, row_count=len(rows))


def _read_csv_text(text: str) -> Table:
    sample = text[:8192]
    try:
        dialect: Any = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    try:
        header = next(reader)
    except StopIteration:
        return Table(columns=[], data={}, row_count=0)
    raw_cols = [h.strip() for h in header]
    # Deduplicate column names: e.g. id, id -> id, id_2
    seen_cols: Dict[str, int] = {}
    cols: List[str] = []
    for h in raw_cols:
        if h in seen_cols:
            seen_cols[h] += 1
            cols.append(f"{h}_{seen_cols[h]}")
        else:
            seen_cols[h] = 1
            cols.append(h)
    data: Dict[str, List[Any]] = {c: [] for c in cols}
    n = 0
    for row in reader:
        if not row or all(cell.strip() == "" for cell in row):
            continue
        n += 1
        for i, c in enumerate(cols):
            raw = row[i] if i < len(row) else None
            data[c].append(_coerce(raw))
    return Table(columns=cols, data=data, row_count=n)


def load_csv_text(text: str) -> Table:
    return _read_csv_text(text)


def _load_with_stdlib(path: str) -> Table:
    with open(path, "r", newline="", encoding="utf-8-sig") as fh:
        return _read_csv_text(fh.read())


def load_table(path: str, prefer_duckdb: bool = True) -> Tuple[Table, str]:
    """Return (table, engine_name). DuckDB used when available + preferred."""
    if prefer_duckdb:
        tbl = _load_with_duckdb(path)
        if tbl is not None:
            return tbl, "duckdb"
    return _load_with_stdlib(path), "stdlib-csv"


# --------------------------------------------------------------------------- #
# Check parsing
# --------------------------------------------------------------------------- #
_NUM = r"[+-]?\d+(?:\.\d+)?"


def _num(s: str) -> Any:
    return int(s) if re.fullmatch(r"[+-]?\d+", s) else float(s)


def _split_severity(line: str) -> Tuple[str, str]:
    m = re.search(r"\[(error|warn)\]\s*$", line, re.IGNORECASE)
    if m:
        return line[: m.start()].strip(), m.group(1).lower()
    return line, "error"


def _split_filter(line: str) -> Tuple[str, Optional[Tuple[str, str, str]]]:
    """Pull a trailing ``where COL OP VALUE`` partition filter off a check."""
    m = re.search(r"\s+where\s+(\S+)\s*(<=|>=|!=|<|>|==|=)\s*(.+)$", line, re.IGNORECASE)
    if not m:
        return line, None
    col, op, val = m.group(1), m.group(2), m.group(3).strip()
    return line[: m.start()].strip(), (col, op, val)


def parse_checks(text: str) -> List[Check]:
    checks: List[Check] = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        body, sev = _split_severity(line)
        body, filt = _split_filter(body)
        chk = _parse_one(body)
        chk.severity = sev
        chk.filter = filt
        chk.raw = line
        checks.append(chk)
    return checks


def _parse_one(line: str) -> Check:
    rules: List[Tuple[str, str, Callable[[Any], Dict[str, Any]]]] = [
        (rf"row_count\s+between\s+({_NUM})\s+and\s+({_NUM})", "row_count_between",
         lambda m: {"lo": _num(m.group(1)), "hi": _num(m.group(2))}),
        (rf"row_count\s*(<=|>=|<|>|==|=)\s*({_NUM})", "row_count_cmp",
         lambda m: {"op": m.group(1), "value": _num(m.group(2))}),

        (r"not_null\s+(\S+)", "not_null", lambda m: {"column": m.group(1)}),
        (rf"null_percent\s+(\S+)\s*(<=|>=|<|>|==|=)\s*({_NUM})", "null_percent",
         lambda m: {"column": m.group(1), "op": m.group(2), "value": _num(m.group(3))}),
        (rf"completeness\s+(\S+)\s*(<=|>=|<|>|==|=)\s*({_NUM})", "completeness",
         lambda m: {"column": m.group(1), "op": m.group(2), "value": _num(m.group(3))}),

        (r"unique\s+(\S+)", "unique", lambda m: {"columns": [m.group(1)]}),
        (r"no_duplicates\s+(.+)", "unique",
         lambda m: {"columns": [c.strip() for c in m.group(1).split(",") if c.strip()]}),
        (rf"duplicate_percent\s+(\S+)\s*(<=|>=|<|>|==|=)\s*({_NUM})", "duplicate_percent",
         lambda m: {"column": m.group(1), "op": m.group(2), "value": _num(m.group(3))}),

        (rf"percentile\s+(\S+)\s+p(\d{{1,3}})\s+between\s+({_NUM})\s+and\s+({_NUM})",
         "percentile_between",
         lambda m: {"column": m.group(1), "p": min(int(m.group(2)), 100),
                    "lo": _num(m.group(3)), "hi": _num(m.group(4))}),
        (rf"percentile\s+(\S+)\s+p(\d{{1,3}})\s*(<=|>=|<|>|==|=)\s*({_NUM})",
         "percentile_cmp",
         lambda m: {"column": m.group(1), "p": min(int(m.group(2)), 100),
                    "op": m.group(3), "value": _num(m.group(4))}),

        (rf"(min|max|avg|sum|stddev|median)\s+(\S+)\s+between\s+({_NUM})\s+and\s+({_NUM})",
         "agg_between",
         lambda m: {"agg": m.group(1), "column": m.group(2),
                    "lo": _num(m.group(3)), "hi": _num(m.group(4))}),
        (rf"(min|max|avg|sum|stddev|median)\s+(\S+)\s*(<=|>=|<|>|==|=)\s*({_NUM})",
         "agg_cmp",
         lambda m: {"agg": m.group(1), "column": m.group(2),
                    "op": m.group(3), "value": _num(m.group(4))}),

        (r"accepted_values\s+(\S+)\s+in\s+(.+)", "accepted_values",
         lambda m: {"column": m.group(1),
                    "values": [v.strip() for v in m.group(2).split(",") if v.strip()]}),
        (r"invalid_values\s+(\S+)\s+in\s+(.+)", "invalid_values",
         lambda m: {"column": m.group(1),
                    "values": [v.strip() for v in m.group(2).split(",") if v.strip()]}),

        (r"matches_regex\s+(\S+)\s+(.+)", "matches_regex",
         lambda m: {"column": m.group(1), "pattern": m.group(2)}),
        (r"invalid_regex\s+(\S+)\s+(.+)", "matches_regex",
         lambda m: {"column": m.group(1), "pattern": m.group(2)}),

        (rf"length\s+(\S+)\s+between\s+({_NUM})\s+and\s+({_NUM})", "length_between",
         lambda m: {"column": m.group(1), "lo": _num(m.group(2)), "hi": _num(m.group(3))}),
        (rf"in_range\s+(\S+)\s+between\s+({_NUM})\s+and\s+({_NUM})", "in_range",
         lambda m: {"column": m.group(1), "lo": _num(m.group(2)), "hi": _num(m.group(3))}),

        (r"monotonic_increasing\s+(\S+)", "monotonic",
         lambda m: {"column": m.group(1)}),

        (rf"freshness\s+(\S+)\s*<=\s*({_NUM})", "freshness",
         lambda m: {"column": m.group(1), "max_days": _num(m.group(2))}),

        (r"schema\s+(.+)", "schema",
         lambda m: {"columns": [c.strip() for c in m.group(1).split(",") if c.strip()]}),

        # ---- cross-column row expression --------------------------------- #
        (r"row_expr\s+(.+)", "row_expr", lambda m: {"expr": m.group(1).strip()}),

        # ---- referential integrity --------------------------------------- #
        (r"reference\s+(\S+)\s+in\s+(\S+):(\S+)", "reference",
         lambda m: {"column": m.group(1), "ref_path": m.group(2),
                    "ref_column": m.group(3)}),

        # ---- group-by: every group must satisfy the inner predicate ------ #
        (rf"group_by\s+(\S+)\s+row_count\s*(<=|>=|<|>|==|=)\s*({_NUM})",
         "group_by_row_count",
         lambda m: {"group": m.group(1), "op": m.group(2), "value": _num(m.group(3))}),
        (rf"group_by\s+(\S+)\s+(min|max|avg|sum)\s+(\S+)\s*(<=|>=|<|>|==|=)\s*({_NUM})",
         "group_by_agg",
         lambda m: {"group": m.group(1), "agg": m.group(2), "column": m.group(3),
                    "op": m.group(4), "value": _num(m.group(5))}),

        # ---- anomaly / change detection vs the metric store -------------- #
        (rf"anomaly\s+(row_count|min|max|avg|sum|stddev|median)(?:\s+(\S+))?\s+"
         rf"change\s*(<=|<)\s*({_NUM})%",
         "anomaly",
         lambda m: {"metric": m.group(1), "column": m.group(2),
                    "op": m.group(3), "max_pct": _num(m.group(4))}),
    ]
    for pattern, kind, build in rules:
        m = re.fullmatch(pattern, line)
        if m:
            return Check(line, kind, "error", build(m))
    raise ValueError(f"Cannot parse check: {line!r}")


# --------------------------------------------------------------------------- #
# Check evaluation helpers
# --------------------------------------------------------------------------- #
_OPS: Dict[str, Callable[[Any, Any], bool]] = {
    "<": lambda a, b: a < b,
    ">": lambda a, b: a > b,
    "<=": lambda a, b: a <= b,
    ">=": lambda a, b: a >= b,
    "==": lambda a, b: a == b,
    "=": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}

_DATE_FORMATS = (
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
    "%m/%d/%Y", "%Y/%m/%d", "%d-%m-%Y",
)


def _parse_date(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo is None else value.astimezone(timezone.utc).replace(tzinfo=None)
    if value is None:
        return None
    s = str(value).strip().replace("Z", "")
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _nums(values: List[Any]) -> List[float]:
    return [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]


def _now() -> datetime:
    return datetime.now()


def _percentile(sorted_nums: List[float], p: int) -> float:
    if not sorted_nums:
        raise ValueError("empty")
    if len(sorted_nums) == 1:
        return float(sorted_nums[0])
    rank = (p / 100.0) * (len(sorted_nums) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return float(sorted_nums[lo])
    frac = rank - lo
    return float(sorted_nums[lo] + (sorted_nums[hi] - sorted_nums[lo]) * frac)


def _aggregate(agg: str, nums: List[float]) -> float:
    if agg == "min":
        return min(nums)
    if agg == "max":
        return max(nums)
    if agg == "avg":
        return round(statistics.fmean(nums), 6)
    if agg == "sum":
        return round(sum(nums), 6)
    if agg == "median":
        return round(statistics.median(nums), 6)
    # stddev
    return round(statistics.pstdev(nums), 6) if len(nums) > 1 else 0.0


def _coerce_literal(s: str) -> Any:
    return _coerce(s)


# ---- tiny safe expression evaluator for row_expr -------------------------- #
import ast as _ast  # noqa: E402

_ALLOWED_NODES = (
    _ast.Expression, _ast.BoolOp, _ast.BinOp, _ast.UnaryOp, _ast.Compare,
    _ast.Name, _ast.Load, _ast.Constant, _ast.And, _ast.Or, _ast.Not,
    _ast.Add, _ast.Sub, _ast.Mult, _ast.Div, _ast.Mod, _ast.Pow,
    _ast.USub, _ast.UAdd, _ast.Eq, _ast.NotEq, _ast.Lt, _ast.LtE,
    _ast.Gt, _ast.GtE, _ast.FloorDiv,
)


def _compile_row_expr(expr: str) -> Tuple[Any, List[str]]:
    tree = _ast.parse(expr, mode="eval")
    names: List[str] = []
    for node in _ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ValueError(f"unsupported expression element: {type(node).__name__}")
        if isinstance(node, _ast.Name):
            names.append(node.id)
    code = compile(tree, "<row_expr>", "eval")
    return code, names


def _eval_row_expr(code: Any, names: List[str], table: Table) -> Tuple[int, int]:
    """Return (failing_rows, evaluated_rows). Rows with a null in any used
    column or arithmetic error are skipped (treated as not-evaluable)."""
    failing = 0
    evaluated = 0
    cols = {n: table.data.get(n) for n in names}
    missing = [n for n, v in cols.items() if v is None]
    if missing:
        raise KeyError(missing[0])
    for i in range(table.row_count):
        env = {n: cols[n][i] for n in names}
        if any(v is None for v in env.values()):
            continue
        try:
            ok = bool(eval(code, {"__builtins__": {}}, env))  # noqa: S307 - sandboxed AST
        except (ZeroDivisionError, TypeError):
            continue
        evaluated += 1
        if not ok:
            failing += 1
    return failing, evaluated


# --------------------------------------------------------------------------- #
# Check evaluation
# --------------------------------------------------------------------------- #
def _apply_filter(table: Table, filt: Tuple[str, str, str]) -> Table:
    col, op, raw = filt
    if col not in table.data:
        raise KeyError(col)
    want = _coerce_literal(raw)
    fn = _OPS[op]
    column = table.data[col]

    def keep(i: int) -> bool:
        v = column[i]
        if v is None:
            return False
        try:
            return fn(v, want)
        except TypeError:
            return fn(str(v), str(want))

    return table.filtered(keep)


def _eval_one(table: Table, c: Check, ctx: Optional["EvalContext"] = None) -> CheckResult:
    kind, a = c.kind, c.args

    def res(passed: bool, observed: Any, failing: int = 0, detail: str = "") -> CheckResult:
        return CheckResult(c.raw, kind, c.severity, passed, observed, failing, detail)

    def missing(col: str) -> CheckResult:
        return res(False, None, 0, f"column {col!r} not found")

    # Checks that must see the *unfiltered* whole table.
    if kind == "reference":
        return _eval_reference(table, c, ctx, res, missing)
    if kind == "anomaly":
        return _eval_anomaly(table, c, ctx, res)
    if kind.startswith("group_by"):
        return _eval_group_by(table, c, res, missing)

    # Apply a partition filter to everything else.
    if c.filter is not None:
        try:
            table = _apply_filter(table, c.filter)
        except KeyError as e:
            return missing(str(e).strip("'\""))

    # ---- row count ---------------------------------------------------------
    if kind == "row_count_cmp":
        obs = table.row_count
        return res(_OPS[a["op"]](obs, a["value"]), obs)
    if kind == "row_count_between":
        obs = table.row_count
        return res(a["lo"] <= obs <= a["hi"], obs)

    # ---- schema ------------------------------------------------------------
    if kind == "schema":
        missing_cols = [col for col in a["columns"] if col not in table.data]
        return res(not missing_cols, sorted(missing_cols), len(missing_cols),
                   f"missing columns: {', '.join(missing_cols)}" if missing_cols else "")

    # ---- cross-column row expression ---------------------------------------
    if kind == "row_expr":
        try:
            code, names = _compile_row_expr(a["expr"])
        except (SyntaxError, ValueError) as e:
            return res(False, None, 0, f"bad expression: {e}")
        try:
            failing, evaluated = _eval_row_expr(code, names, table)
        except KeyError as e:
            return missing(str(e).strip("'\""))
        return res(failing == 0, failing, failing,
                   f"{failing} of {evaluated} evaluable row(s) violate expression")

    col = a.get("column")
    if col is not None and col not in table.data:
        return missing(col)

    # ---- completeness family ----------------------------------------------
    if kind == "not_null":
        nulls = sum(1 for v in table.col(col) if v is None)
        return res(nulls == 0, nulls, nulls, f"{nulls} null value(s)")

    if kind in ("null_percent", "completeness"):
        n = max(table.row_count, 1)
        nulls = sum(1 for v in table.col(col) if v is None)
        if kind == "null_percent":
            obs = round(100.0 * nulls / n, 4)
        else:
            obs = round(100.0 * (n - nulls) / n, 4)
        return res(_OPS[a["op"]](obs, a["value"]), obs, nulls,
                   f"{nulls} null of {table.row_count} rows")

    # ---- uniqueness --------------------------------------------------------
    if kind == "unique":
        cols = a["columns"]
        for cc in cols:
            if cc not in table.data:
                return missing(cc)
        seen: Dict[Tuple, int] = {}
        for key in zip(*(table.col(cc) for cc in cols)):
            seen[key] = seen.get(key, 0) + 1
        dups = sum(v - 1 for v in seen.values() if v > 1)
        return res(dups == 0, dups, dups, f"{dups} duplicate row(s) on {','.join(cols)}")

    if kind == "duplicate_percent":
        n = max(table.row_count, 1)
        seen2: Dict[Any, int] = {}
        for v in table.col(col):
            seen2[v] = seen2.get(v, 0) + 1
        dups = sum(v - 1 for v in seen2.values() if v > 1)
        obs = round(100.0 * dups / n, 4)
        return res(_OPS[a["op"]](obs, a["value"]), obs, dups, f"{dups} duplicate value(s)")

    # ---- percentiles -------------------------------------------------------
    if kind in ("percentile_cmp", "percentile_between"):
        nums = sorted(_nums(table.col(col)))
        if not nums:
            return res(False, None, 0, f"no numeric values in {col!r}")
        obs = round(_percentile(nums, a["p"]), 6)
        if kind == "percentile_between":
            return res(a["lo"] <= obs <= a["hi"], obs)
        return res(_OPS[a["op"]](obs, a["value"]), obs)

    # ---- aggregates --------------------------------------------------------
    if kind in ("agg_cmp", "agg_between"):
        nums = _nums(table.col(col))
        if not nums:
            return res(False, None, 0, f"no numeric values in {col!r}")
        obs = _aggregate(a["agg"], nums)
        if kind == "agg_between":
            return res(a["lo"] <= obs <= a["hi"], obs)
        return res(_OPS[a["op"]](obs, a["value"]), obs)

    # ---- value membership --------------------------------------------------
    if kind == "accepted_values":
        allowed = set(a["values"])
        bad = sorted({str(v) for v in table.col(col)
                      if v is not None and str(v) not in allowed})
        cnt = sum(1 for v in table.col(col) if v is not None and str(v) not in allowed)
        return res(not bad, bad, cnt, f"{cnt} row(s) with unexpected value(s)")

    if kind == "invalid_values":
        banned = set(a["values"])
        cnt = sum(1 for v in table.col(col) if v is not None and str(v) in banned)
        return res(cnt == 0, cnt, cnt, f"{cnt} row(s) contain a banned value")

    # ---- regex -------------------------------------------------------------
    if kind == "matches_regex":
        try:
            pat = re.compile(a["pattern"])
        except re.error as e:
            return res(False, None, 0, f"bad regex: {e}")
        bad = sum(1 for v in table.col(col) if v is not None and not pat.search(str(v)))
        return res(bad == 0, bad, bad, f"{bad} value(s) failed pattern")

    # ---- string length -----------------------------------------------------
    if kind == "length_between":
        bad = sum(1 for v in table.col(col)
                  if v is not None and not (a["lo"] <= len(str(v)) <= a["hi"]))
        return res(bad == 0, bad, bad, f"{bad} value(s) outside length range")

    # ---- numeric per-row range ---------------------------------------------
    if kind == "in_range":
        nums = _nums(table.col(col))
        bad = sum(1 for v in nums if not (a["lo"] <= v <= a["hi"]))
        return res(bad == 0, bad, bad, f"{bad} numeric value(s) out of range")

    # ---- ordering ----------------------------------------------------------
    if kind == "monotonic":
        nums = _nums(table.col(col))
        bad = sum(1 for i in range(1, len(nums)) if nums[i] < nums[i - 1])
        return res(bad == 0, bad, bad, f"{bad} decrease(s) detected")

    # ---- freshness ---------------------------------------------------------
    if kind == "freshness":
        dates = [d for d in (_parse_date(v) for v in table.col(col)) if d is not None]
        if not dates:
            return res(False, None, 0, f"no parseable dates in {col!r}")
        age = round((_now() - max(dates)).total_seconds() / 86400.0, 3)
        return res(age <= a["max_days"], age, 0, f"newest row is {age} day(s) old")

    return res(False, None, 0, "unknown check kind")


def _eval_reference(table, c, ctx, res, missing) -> CheckResult:
    a = c.args
    col = a["column"]
    if col not in table.data:
        return missing(col)
    if c.filter is not None:
        try:
            table = _apply_filter(table, c.filter)
        except KeyError as e:
            return missing(str(e).strip("'\""))
    ref_path = a["ref_path"]
    if ctx and not os.path.isabs(ref_path):
        ref_path = os.path.join(ctx.base_dir, ref_path)
    try:
        ref_table, _ = load_table(ref_path, prefer_duckdb=False)
    except OSError as e:
        return res(False, None, 0, f"cannot read reference {ref_path!r}: {e}")
    if a["ref_column"] not in ref_table.data:
        return res(False, None, 0,
                   f"reference column {a['ref_column']!r} not in {ref_path!r}")
    valid = {v for v in ref_table.col(a["ref_column"]) if v is not None}
    orphans = [v for v in table.col(col) if v is not None and v not in valid]
    cnt = len(orphans)
    sample = sorted({str(o) for o in orphans})[:5]
    return res(cnt == 0, cnt, cnt,
               f"{cnt} value(s) absent from {os.path.basename(a['ref_path'])}:"
               f"{a['ref_column']}" + (f" e.g. {sample}" if sample else ""))


def _eval_group_by(table, c, res, missing) -> CheckResult:
    a = c.args
    group = a["group"]
    if group not in table.data:
        return missing(group)
    if c.filter is not None:
        try:
            table = _apply_filter(table, c.filter)
        except KeyError as e:
            return missing(str(e).strip("'\""))
    # bucket row indices by group key
    buckets: Dict[Any, List[int]] = {}
    gcol = table.col(group)
    for i, key in enumerate(gcol):
        buckets.setdefault(key, []).append(i)

    op = a["op"]
    fn = _OPS[op]
    failed_groups: List[str] = []
    worst: Any = None

    if c.kind == "group_by_row_count":
        for key, idx in buckets.items():
            obs = len(idx)
            if not fn(obs, a["value"]):
                failed_groups.append(f"{key}={obs}")
                worst = obs if worst is None else worst
        return res(not failed_groups, len(buckets) - len(failed_groups),
                   len(failed_groups),
                   f"{len(failed_groups)} group(s) fail row_count {op} {a['value']}: "
                   f"{failed_groups[:5]}" if failed_groups else "")

    # group_by_agg
    col = a["column"]
    if col not in table.data:
        return missing(col)
    ccol = table.col(col)
    for key, idx in buckets.items():
        nums = _nums([ccol[i] for i in idx])
        if not nums:
            continue
        obs = _aggregate(a["agg"], nums)
        if not fn(obs, a["value"]):
            failed_groups.append(f"{key}={obs}")
    return res(not failed_groups, len(buckets) - len(failed_groups), len(failed_groups),
               f"{len(failed_groups)} group(s) fail {a['agg']} {col} {op} {a['value']}: "
               f"{failed_groups[:5]}" if failed_groups else "")


def _compute_metric(table: Table, metric: str, column: Optional[str]) -> Optional[float]:
    if metric == "row_count":
        return float(table.row_count)
    if column is None or column not in table.data:
        return None
    nums = _nums(table.col(column))
    if not nums:
        return None
    return float(_aggregate(metric, nums))


def _eval_anomaly(table, c, ctx, res) -> CheckResult:
    a = c.args
    metric, column = a["metric"], a.get("column")
    key = metric if column is None else f"{metric}:{column}"
    current = _compute_metric(table, metric, column)
    if current is None:
        return res(False, None, 0, f"cannot compute metric {key!r}")
    if ctx is not None:
        ctx.record_metric(key, current)
    baseline = ctx.baseline.get(key) if (ctx and ctx.baseline) else None
    if baseline is None:
        # No history yet: a baseline-establishing run always passes.
        return res(True, round(current, 6), 0,
                   "no baseline yet (recorded for next run)")
    if baseline == 0:
        pct = 0.0 if current == 0 else 100.0
    else:
        pct = abs(current - baseline) / abs(baseline) * 100.0
    pct = round(pct, 4)
    ok = _OPS[a["op"]](pct, a["max_pct"])
    return res(ok, pct, 0,
               f"{key} moved {pct}% (baseline={round(baseline,4)} -> "
               f"current={round(current,4)}; allowed {a['op']} {a['max_pct']}%)")


# --------------------------------------------------------------------------- #
# Metric store (soda-core style history) + evaluation context
# --------------------------------------------------------------------------- #
@dataclass
class EvalContext:
    base_dir: str = "."
    baseline: Dict[str, float] = field(default_factory=dict)
    recorded: Dict[str, float] = field(default_factory=dict)

    def record_metric(self, key: str, value: float) -> None:
        self.recorded[key] = value


def load_metric_store(path: str) -> Dict[str, float]:
    import json
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return {str(k): float(v) for k, v in data.get("metrics", {}).items()}


def save_metric_store(path: str, metrics: Dict[str, float]) -> None:
    import json
    import warnings
    payload = {"tool": TOOL_NAME, "version": TOOL_VERSION,
               "updated_at": _now().isoformat(timespec="seconds"),
               "metrics": metrics}
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
    except OSError as exc:
        warnings.warn(
            f"duckprobe: could not write metric store {path!r}: {exc}",
            RuntimeWarning, stacklevel=2)


def run_checks(table: Table, checks: List[Check],
               ctx: Optional[EvalContext] = None) -> List[CheckResult]:
    return [_eval_one(table, c, ctx) for c in checks]


def probe(path: str, check_text: str, prefer_duckdb: bool = True,
          metric_store: Optional[str] = None) -> ProbeReport:
    table, engine = load_table(path, prefer_duckdb=prefer_duckdb)
    ctx = EvalContext(
        base_dir=os.path.dirname(os.path.abspath(path)) or ".",
        baseline=load_metric_store(metric_store) if metric_store else {},
    )
    results = run_checks(table, parse_checks(check_text), ctx)
    if metric_store and ctx.recorded:
        merged = dict(ctx.baseline)
        merged.update(ctx.recorded)
        save_metric_store(metric_store, merged)
    return ProbeReport(path, engine, table.row_count, results, dict(ctx.recorded))


# --------------------------------------------------------------------------- #
# Auto-profiling -> suggested checks
# --------------------------------------------------------------------------- #
def profile_table(table: Table) -> Dict[str, Any]:
    """Return a per-column profile (counts, type, distinct, min/max/avg)."""
    n = table.row_count
    cols: Dict[str, Any] = {}
    for name in table.columns:
        vals = table.data[name]
        non_null = [v for v in vals if v is not None]
        nums = _nums(non_null)
        kind = "numeric" if nums and len(nums) == len(non_null) else (
            "empty" if not non_null else "string")
        info: Dict[str, Any] = {
            "rows": n,
            "nulls": n - len(non_null),
            "null_percent": round(100.0 * (n - len(non_null)) / max(n, 1), 3),
            "distinct": len({str(v) for v in non_null}),
            "type": kind,
        }
        if kind == "numeric":
            info["min"] = min(nums)
            info["max"] = max(nums)
            info["avg"] = round(statistics.fmean(nums), 4)
        cols[name] = info
    return {"row_count": n, "columns": cols}


def suggest_checks(table: Table) -> str:
    prof = profile_table(table)
    lines = ["row_count > 0"]
    cols = prof["columns"]
    if table.columns:
        lines.append("schema " + ", ".join(table.columns))
    for name, info in cols.items():
        if info["type"] == "empty":
            continue
        if info["nulls"] == 0:
            lines.append(f"not_null {name}")
        elif info["null_percent"] < 50:
            lines.append(f"null_percent {name} <= {math.ceil(info['null_percent']) + 1} [warn]")
        if info["distinct"] == info["rows"] - info["nulls"] and info["rows"] - info["nulls"] > 1:
            lines.append(f"unique {name}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Bundled, genuinely-useful suite + datasets
# --------------------------------------------------------------------------- #
def bundled_customers_csv() -> str:
    """The referential master: the set of *valid* customer ids/regions."""
    return (
        "id,name,region,tier\n"
        "1001,Alice,US,gold\n"
        "1002,Bob,US,silver\n"
        "1003,Carol,GB,gold\n"
        "1004,Dave,CA,bronze\n"
        "1005,Erin,US,gold\n"
        "1006,Frank,DE,silver\n"
        "1007,Grace,FR,bronze\n"
        "1008,Heidi,US,silver\n"
        "1009,Ivan,US,gold\n"
        "1012,Judy,CA,bronze\n"
        "1013,Mallory,US,gold\n"
        "1014,Niaj,US,silver\n"
        "1015,Olivia,DE,gold\n"
    )


def bundled_dataset_csv() -> str:
    """A realistic 'orders' extract with intentionally injected DQ issues.

    Issues present (so the bundled suite produces a mix of pass/fail/warn):
      * duplicate customer_id (1001 appears twice)
      * a null email (row for 1007)
      * an out-of-range age (200) and a negative balance
      * a status value 'unknown' not in the accepted set
      * a malformed email 'not-an-email'
      * an orphan customer_id (9999) absent from the customers master
      * a row where balance != age * 10 would be caught by row_expr in the
        demo suite (the bundled suite keeps balance independent)
    """
    return (
        "customer_id,email,signup_date,age,country,status,balance\n"
        "1001,alice@example.com,2026-05-01,34,US,active,1240.50\n"
        "1002,bob@example.com,2026-05-02,29,US,active,82.00\n"
        "1003,carol@example.org,2026-05-03,41,GB,churned,0.00\n"
        "1004,dave@example.net,2026-05-04,23,CA,trial,15.25\n"
        "1005,erin@example.com,2026-05-05,52,US,active,9800.10\n"
        "1006,frank@example.io,2026-05-06,38,DE,active,540.00\n"
        "1007,,2026-05-07,45,FR,active,310.75\n"
        "1008,grace@example.com,2026-05-08,200,US,active,77.00\n"
        "1009,heidi@example.com,2026-05-09,31,US,unknown,-50.00\n"
        "1010,not-an-email,2026-05-10,27,GB,trial,5.00\n"
        "1001,alice2@example.com,2026-05-11,35,US,active,12.00\n"
        "1012,judy@example.com,2026-05-12,60,CA,churned,430.00\n"
        "1013,mallory@example.com,2026-05-13,44,US,active,1200.00\n"
        "1014,niaj@example.org,2026-05-14,19,US,trial,3.50\n"
        "9999,zane@example.com,2026-05-15,37,DE,active,640.20\n"
    )


def bundled_suite() -> str:
    """The default check suite shipped with duckprobe (Soda-Core flavored)."""
    return (
        "# duckprobe bundled data-quality suite (orders extract)\n"
        "schema customer_id, email, status, age, balance\n"
        "row_count between 1 and 1000000\n"
        "not_null customer_id\n"
        "unique customer_id\n"
        "duplicate_percent customer_id < 1\n"
        "completeness email >= 95 [warn]\n"
        "not_null email\n"
        "matches_regex email ^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$\n"
        "accepted_values status in active, churned, trial\n"
        "in_range age between 0 and 120\n"
        "min balance >= 0\n"
        "avg age between 18 and 90 [warn]\n"
        "percentile balance p90 < 100000 [warn]\n"
        "group_by country row_count >= 1\n"
        "avg balance < 5000 where status = active [warn]\n"
    )


def bundled_report(prefer_duckdb: bool = False) -> ProbeReport:
    """Run the bundled suite against the bundled dataset entirely in memory."""
    import tempfile
    table = load_csv_text(bundled_dataset_csv())
    with tempfile.TemporaryDirectory(prefix="duckprobe_") as d:
        cust_path = os.path.join(d, "customers.csv")
        with open(cust_path, "w", encoding="utf-8") as fh:
            fh.write(bundled_customers_csv())
        suite = bundled_suite() + f"\nreference customer_id in {cust_path}:id\n"
        ctx = EvalContext(base_dir=d)
        results = run_checks(table, parse_checks(suite), ctx)
    return ProbeReport("<bundled:orders>", "stdlib-csv", table.row_count,
                       results, dict(ctx.recorded))


__all__ = [
    "TOOL_NAME", "TOOL_VERSION", "SEVERITIES",
    "Table", "Check", "CheckResult", "ProbeReport", "EvalContext",
    "load_table", "load_csv_text", "parse_checks", "run_checks", "probe",
    "profile_table", "suggest_checks",
    "load_metric_store", "save_metric_store",
    "bundled_dataset_csv", "bundled_customers_csv", "bundled_suite", "bundled_report",
]
