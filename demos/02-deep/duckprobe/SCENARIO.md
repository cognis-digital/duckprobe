# duckprobe deep demo — data-quality gating an orders extract

This demo shows `duckprobe` acting like a Soda-Core scan in CI: a daily
`orders` CSV is validated against a human-readable check suite with per-check
**severity** (`error` blocks the pipeline, `[warn]` only reports). It exercises
the full feature set — not just column metrics, but the things that make
soda-core a real data-quality tool:

- **Filtered / partitioned checks** (`... where region = EU`)
- **Cross-column row expressions** (`row_expr total == quantity * unit_price`)
- **Referential integrity** against a master file (`reference ... in customers.csv:customer_id`)
- **Percentiles, median, group-by** (`percentile total p90 ...`, `group_by region row_count >= 1`)
- **Anomaly / change detection** against a JSON metric store (`anomaly avg total change < 30%`)

## Files
- `orders.csv` — a 15-row extract with intentionally injected issues.
- `customers.csv` — the referential master of known customer emails/regions.
- `orders.checks` — the data-quality suite (DSL, one check per line).

## Injected issues (and which checks catch them)
| Issue in `orders.csv`                         | Check that fires                                  | Severity |
|-----------------------------------------------|---------------------------------------------------|----------|
| `order_id` 50001 appears twice                | `unique order_id`, `duplicate_percent order_id`   | error    |
| empty `customer_email` on order 50006         | `not_null customer_email`                         | error    |
| `bad-email` on order 50010                    | `matches_regex customer_email ...`                | error    |
| `bad-email` not in the customer master        | `reference customer_email in customers.csv:...`   | error    |
| `status = frozen` on order 50011              | `accepted_values status in ...`                   | error    |
| `quantity = -1` on order 50008                | `in_range quantity between 1 and 100`             | error    |
| `total = -8.00` on order 50008                | `min total >= 0`                                  | error    |

The cross-column invariant `total == quantity * unit_price` actually *holds*
across all rows here, so `row_expr` **passes** — demonstrating a green
structural check alongside the failures.

## Run it

```console
$ python -m duckprobe check demos/02-deep/duckprobe/orders.csv \
      --checks demos/02-deep/duckprobe/orders.checks
duckprobe FAIL  source=...orders.csv  engine=stdlib-csv  rows=15
----------------------------------------------------------------------------
[ok  ] schema order_id, customer_email, status, quantity, total
[FAIL] unique order_id   observed=1   (1 duplicate row(s) on order_id)
[ok  ] min total >= 0 where region = EU   observed=37.5
[ok  ] row_expr total == quantity * unit_price   observed=0
[FAIL] reference customer_email in customers.csv:customer_id   observed=1
       (1 value(s) absent from customers.csv:customer_id e.g. ['bad-email'])
...
$ echo $?
1
```

JSON output for machine consumption, or JUnit XML for a CI gate:

```console
$ python -m duckprobe --format json  check orders.csv --checks orders.checks
$ python -m duckprobe --format junit check orders.csv --checks orders.checks > results.xml
```

### Anomaly / change detection

Point a run at a metric store; the first run establishes a baseline, later runs
compare against it (this is soda-core's signature capability, here with no
external service — just a JSON file):

```console
$ python -m duckprobe check orders.csv --checks orders.checks \
      --metric-store .duckprobe_history.json
# first run: anomaly checks pass and record metrics
# next day, if avg total swings > 30% vs the recorded baseline -> the
# `anomaly avg total change < 30%` check fails.
```

A clean extract returns exit code `0`; any `error`-severity failure returns `1`.
Try the built-in self-test suite with no files at all (it ships its own orders
+ customers datasets and runs every check kind, including referential
integrity):

```console
$ python -m duckprobe scan
```
