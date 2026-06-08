# Demo 01 — Basic file data-quality probe

You just received `customers.csv`, an extract from an upstream system, and you
want to gate it before loading into your warehouse. DUCKPROBE lets you express
checks in a tiny human-readable DSL and get a pass/fail report — no schema
registration, no services, zero install.

## Files
- `customers.csv` — 8-row customer extract
- `customers.checks` — the data-quality contract

## Run it

```bash
# From the build_out directory:
python -m duckprobe check demos/01-basic/customers.csv \
    --checks demos/01-basic/customers.checks

# JSON for CI / pipelines (exit code 1 if any check fails):
python -m duckprobe check demos/01-basic/customers.csv \
    --checks demos/01-basic/customers.checks --format json
```

Expected: every check passes, exit code `0`.

## Inline checks (no file needed)

```bash
python -m duckprobe check demos/01-basic/customers.csv \
    -c "row_count > 0" -c "unique id" -c "not_null email"
```

## Auto-profile a file

Don't know what to check yet? Let DUCKPROBE propose a starting contract:

```bash
python -m duckprobe checks demos/01-basic/customers.csv
```

It profiles every column and emits `not_null` / `unique` / `row_count` checks
for the columns that currently satisfy them — a baseline you can paste into a
`.checks` file and harden over time.

## Engine

If `duckdb` is installed, DUCKPROBE reads the file with DuckDB (so Parquet/JSON
work too). Otherwise it falls back to a pure-stdlib CSV engine with type
sniffing — the checks and output are identical either way. Force the fallback
with `--no-duckdb`.

## Failing example

Add a bad check to see a non-zero exit:

```bash
python -m duckprobe check demos/01-basic/customers.csv -c "max age <= 30"
echo $?   # -> 1
```
