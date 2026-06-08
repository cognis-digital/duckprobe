"""DUCKPROBE engine.

A *table* is loaded into memory as a list of column-oriented values plus a row
count. Checks are evaluated against that table. When the optional ``duckdb``
package is present we let DuckDB read the file (Parquet/CSV/JSON/etc.) and run
aggregate SQL; otherwise we use a pure-stdlib CSV reader with type sniffing so
the tool works with zero dependencies.

Check DSL (one per line, ``#`` comments allowed)::

    row_count > 0
    row_count between 1 and 1000
    not_null email
    unique id
    no_duplicates id, region          # combined key
    min age >= 0
    max age <= 130
    accepted_values status in active, churned, trial
    matches_regex email ^[^@]+@[^@]+$
    freshness updated_at <= 7          # days since max(updated_at)
"""

from __future__ import annotations

import csv
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class Table:
    columns: List[str]
    # column name -> list of python values (None for missing/blank)
    data: Dict[str, List[Any]]
    row_count: int

    def col(self, name: str) -> List[Any]:
        if name not in self.data:
            raise KeyError(name)
        return self.data[name]


@dataclass
class Check:
    raw: str
    kind: str
    args: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CheckResult:
    check: str
    kind: str
    passed: bool
    observed: Any
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "check": self.check,
            "kind": self.kind,
            "passed": self.passed,
            "observed": self.observed,
            "detail": self.detail,
        }


@dataclass
class ProbeReport:
    source: str
    engine: str
    row_count: int
    results: List[CheckResult]

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results)

    @property
    def failed_count(self) -> int:
        return sum(1 for r in self.results if not r.passed)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "engine": self.engine,
            "row_count": self.row_count,
            "checks_total": len(self.results),
            "checks_failed": self.failed_count,
            "passed": self.passed,
            "results": [r.to_dict() for r in self.results],
        }


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def _coerce(value: str) -> Any:
    """Sniff a raw string into int / float / bool / None / str."""
    if value is None:
        return None
    s = value.strip()
    if s == "" or s.lower() in ("null", "na", "n/a", "none"):
        return None
    low = s.lower()
    if low in ("true", "false"):
        return low == "true"
    # int
    try:
        if re.fullmatch(r"[+-]?\d+", s):
            return int(s)
    except ValueError:
        pass
    # float
    try:
        f = float(s)
        if not math.isnan(f):
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


def _load_with_stdlib(path: str) -> Table:
    with open(path, "r", newline="", encoding="utf-8-sig") as fh:
        sample = fh.read(8192)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        reader = csv.reader(fh, dialect)
        try:
            header = next(reader)
        except StopIteration:
            return Table(columns=[], data={}, row_count=0)
        cols = [h.strip() for h in header]
        data: Dict[str, List[Any]] = {c: [] for c in cols}
        n = 0
        for row in reader:
            if not row or all(cell.strip() == "" for cell in row):
                continue
            n += 1
            for i, c in enumerate(cols):
                raw = row[i] if i < len(row) else None
                data[c].append(_coerce(raw) if raw is not None else None)
    return Table(columns=cols, data=data, row_count=n)


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


def parse_checks(text: str) -> List[Check]:
    checks: List[Check] = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        checks.append(_parse_one(line))
    return checks


def _num(s: str) -> Any:
    return int(s) if re.fullmatch(r"[+-]?\d+", s) else float(s)


def _parse_one(line: str) -> Check:
    m = re.fullmatch(rf"row_count\s+between\s+({_NUM})\s+and\s+({_NUM})", line)
    if m:
        return Check(line, "row_count_between",
                     {"lo": _num(m.group(1)), "hi": _num(m.group(2))})

    m = re.fullmatch(rf"row_count\s*(<=|>=|<|>|==|=)\s*({_NUM})", line)
    if m:
        return Check(line, "row_count_cmp",
                     {"op": m.group(1), "value": _num(m.group(2))})

    m = re.fullmatch(r"not_null\s+(\S+)", line)
    if m:
        return Check(line, "not_null", {"column": m.group(1)})

    m = re.fullmatch(r"unique\s+(\S+)", line)
    if m:
        return Check(line, "unique", {"columns": [m.group(1)]})

    m = re.fullmatch(r"no_duplicates\s+(.+)", line)
    if m:
        cols = [c.strip() for c in m.group(1).split(",") if c.strip()]
        return Check(line, "unique", {"columns": cols})

    m = re.fullmatch(rf"(min|max)\s+(\S+)\s+(<=|>=|<|>|==|=)\s*({_NUM})", line)
    if m:
        return Check(line, "agg_cmp",
                     {"agg": m.group(1), "column": m.group(2),
                      "op": m.group(3), "value": _num(m.group(4))})

    m = re.fullmatch(r"accepted_values\s+(\S+)\s+in\s+(.+)", line)
    if m:
        vals = [v.strip() for v in m.group(2).split(",") if v.strip()]
        return Check(line, "accepted_values",
                     {"column": m.group(1), "values": vals})

    m = re.fullmatch(r"matches_regex\s+(\S+)\s+(.+)", line)
    if m:
        return Check(line, "matches_regex",
                     {"column": m.group(1), "pattern": m.group(2)})

    m = re.fullmatch(rf"freshness\s+(\S+)\s*<=\s*({_NUM})", line)
    if m:
        return Check(line, "freshness",
                     {"column": m.group(1), "max_days": _num(m.group(2))})

    raise ValueError(f"Cannot parse check: {line!r}")


# --------------------------------------------------------------------------- #
# Check evaluation
# --------------------------------------------------------------------------- #
_OPS = {
    "<": lambda a, b: a < b,
    ">": lambda a, b: a > b,
    "<=": lambda a, b: a <= b,
    ">=": lambda a, b: a >= b,
    "==": lambda a, b: a == b,
    "=": lambda a, b: a == b,
}

_DATE_FORMATS = (
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
    "%m/%d/%Y", "%Y/%m/%d",
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


def _eval_one(table: Table, c: Check) -> CheckResult:
    kind = c.kind
    a = c.args

    def missing_col(col: str) -> CheckResult:
        return CheckResult(c.raw, kind, False, None,
                           f"column {col!r} not found")

    if kind == "row_count_cmp":
        obs = table.row_count
        ok = _OPS[a["op"]](obs, a["value"])
        return CheckResult(c.raw, kind, ok, obs)

    if kind == "row_count_between":
        obs = table.row_count
        ok = a["lo"] <= obs <= a["hi"]
        return CheckResult(c.raw, kind, ok, obs)

    if kind == "not_null":
        col = a["column"]
        if col not in table.data:
            return missing_col(col)
        nulls = sum(1 for v in table.col(col) if v is None)
        return CheckResult(c.raw, kind, nulls == 0, nulls,
                           f"{nulls} null value(s)")

    if kind == "unique":
        cols = a["columns"]
        for col in cols:
            if col not in table.data:
                return missing_col(col)
        seen: Dict[Tuple, int] = {}
        rows = zip(*(table.col(col) for col in cols))
        for key in rows:
            seen[key] = seen.get(key, 0) + 1
        dups = sum(v - 1 for v in seen.values() if v > 1)
        return CheckResult(c.raw, kind, dups == 0, dups,
                           f"{dups} duplicate row(s) on {','.join(cols)}")

    if kind == "agg_cmp":
        col = a["column"]
        if col not in table.data:
            return missing_col(col)
        nums = [v for v in table.col(col) if isinstance(v, (int, float))
                and not isinstance(v, bool)]
        if not nums:
            return CheckResult(c.raw, kind, False, None,
                               f"no numeric values in {col!r}")
        obs = min(nums) if a["agg"] == "min" else max(nums)
        ok = _OPS[a["op"]](obs, a["value"])
        return CheckResult(c.raw, kind, ok, obs)

    if kind == "accepted_values":
        col = a["column"]
        if col not in table.data:
            return missing_col(col)
        allowed = set(a["values"])
        bad = sorted({str(v) for v in table.col(col)
                      if v is not None and str(v) not in allowed})
        return CheckResult(c.raw, kind, not bad, bad,
                           f"{len(bad)} unexpected value(s)")

    if kind == "matches_regex":
        col = a["column"]
        if col not in table.data:
            return missing_col(col)
        try:
            pat = re.compile(a["pattern"])
        except re.error as e:
            return CheckResult(c.raw, kind, False, None, f"bad regex: {e}")
        bad = sum(1 for v in table.col(col)
                  if v is not None and not pat.search(str(v)))
        return CheckResult(c.raw, kind, bad == 0, bad,
                           f"{bad} value(s) failed pattern")

    if kind == "freshness":
        col = a["column"]
        if col not in table.data:
            return missing_col(col)
        dates = [d for d in (_parse_date(v) for v in table.col(col))
                 if d is not None]
        if not dates:
            return CheckResult(c.raw, kind, False, None,
                               f"no parseable dates in {col!r}")
        newest = max(dates)
        age_days = (datetime.utcnow() - newest).total_seconds() / 86400.0
        age = round(age_days, 2)
        return CheckResult(c.raw, kind, age <= a["max_days"], age,
                           f"newest row is {age} day(s) old")

    return CheckResult(c.raw, kind, False, None, "unknown check kind")


def run_checks(table: Table, checks: List[Check]) -> List[CheckResult]:
    return [_eval_one(table, c) for c in checks]
