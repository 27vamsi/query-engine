# query-engine

A small SQL engine that runs analytical queries straight over Parquet files in a
bucket. There's no load step. You point it at a prefix of Parquet, hand it SQL,
and it parses the query, builds a plan, throws away the partitions and columns it
doesn't need, pushes the `WHERE` clause down to the Parquet reader, fans the scan
out across a process pool, and merges the partial results.

The interesting part isn't just that it returns the right answer — it's that you
can see exactly *why* a query was cheap: how many files got skipped, how many
row-groups got pruned, which columns never left disk, and how few compressed
bytes actually came off storage. Athena will tell you it scanned 4.2 MB; it
won't tell you which files it skipped, or why. This does.

Django sits on top as the control plane: register datasets, submit queries, watch
them run, look at the plan as a diagram, and keep a history with timing and
bytes-scanned per query.

## What actually happens to a query

```
SELECT region, SUM(price) AS rev
FROM trips
WHERE region = 'EU' AND price > 50
GROUP BY region
```

1. **Parse** with sqlglot, lower the AST into a logical plan
   (`Scan -> Filter -> Aggregate -> Sort`).
2. **Split the predicate.** `region = 'EU'` only touches a partition column, so it
   becomes a file filter. `price > 50` touches data, so it gets pushed into the
   reader.
3. **Prune.** Drop every file whose path can't match the partition filter (zero
   I/O). For the files that survive, read footers and drop any row-group whose
   `price` min/max can't satisfy `price > 50`.
4. **Project.** Only `price` gets read off disk. `region` comes from the path.
5. **Execute.** One task per (file, surviving row-groups), run across a process
   pool. Each worker does a partial group-by; the coordinator merges them
   (`avg` becomes `sum(sums)/sum(counts)`).
6. **Finish.** Sort, limit, stream back.

The plan, the pruning counts, and the byte numbers all come back with the result.

## Quick start (no infra)

You don't need Docker, Postgres, or MinIO to try it. With nothing configured it
falls back to SQLite and runs Celery tasks inline.

```bash
python -m venv .venv
.venv/bin/pip install -e .          # or .venv\Scripts\pip on Windows
.venv/bin/python scripts/seed_data.py --out ./data/trips --rows 300000 --days 5
.venv/bin/python manage.py migrate
.venv/bin/python manage.py register_dataset trips ./data/trips
.venv/bin/python manage.py createsuperuser
.venv/bin/python manage.py runserver
```

Open http://localhost:8000. There's a `trips` dataset waiting; click into it and
you'll get a guided demo — five one-click queries, each isolating one thing the
engine does. Run the full-scan baseline first, then work down the list:

```
1. Full scan (baseline)   5.90 MB / 5.90 MB   100.0%   nothing pruned
2. Partition pruning        28 KB / 5.97 MB     0.5%   10 of 15 files never opened
3. Row-group skipping      542 KB / 5.90 MB     9.0%   80 of 120 row-groups skipped
4. Column projection      1.37 MB / 5.90 MB    23.2%   1 of 7 columns read
5. All three at once       0.99 MB / 5.97 MB    16.5%   partition + stats + projection
```

Those don't descend in a neat line, and that's the point — each row isolates a
different mechanism rather than stacking on the one before. Steps 2 and 3 are
both row counts, so the gap between them is which column had to be read to
count: step 3 filters on `trip_id` and so pays for a wide int64, while step 2
can count using the narrowest column in the file. Steps 4 and 5 read a real
measure column instead of just counting, which costs more than any amount of
pruning saves.

Each result page explains in English what got skipped and why, alongside the plan
diagram and the raw metrics. There's a SQL box underneath for your own queries.

`/admin` gives you the ops view of every query run. `/api/catalog/datasets/` and
`/api/queries/runs/` are the REST endpoints if you'd rather drive it that way.

## The SQL it understands

A deliberately narrow slice of analytical SQL. Joins and subqueries are out of
scope on purpose, the depth here is in the scan path, not relational breadth.

```sql
SELECT <cols | col AS alias | scalar expr | agg(...)>
FROM <dataset>
[WHERE  <comparisons / IN / BETWEEN / AND / OR / NOT>]
[GROUP BY <cols>]
[ORDER BY <cols> [DESC]]
[LIMIT <n>]
```

Aggregates: `count`, `sum`, `avg`, `min`, `max`. Partition columns
(`region=EU/date=2024-01-01/...`) are first-class in `WHERE`, `GROUP BY`, and
`SELECT` even though they live in the path and never get read off disk.

## What you get back

Every run records:

- `duration_ms`, `rows_returned`, `rows_scanned`
- `files_pruned` (partition pruning)
- `row_groups_pruned` (predicate pushdown against min/max stats)
- `columns_projected` vs `columns_total` (projection pushdown)
- `bytes_scanned` vs `bytes_total` (column pruning + row-group pruning)
- the optimized plan as text and as a Mermaid diagram

`bytes_scanned / bytes_total` is the number to watch, and both sides are
compressed on-disk bytes so a full scan reads exactly 100%. On the sample data a
selective filter like `trip_id > 299990` reads a single row-group out of 120
(0.2% of the dataset), and a `region = 'EU'` query throws away two thirds of the
files before opening anything.

## Object storage

The reader uses PyArrow's filesystem layer, so local paths and S3/MinIO are the
same code path. A dataset URI is just:

```
./data/trips            local
s3://bucket/prefix      S3 or MinIO
```

For MinIO/S3 set `AWS_ENDPOINT_URL`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
`AWS_REGION`. To push the local sample into MinIO:

```bash
python scripts/upload_minio.py --src ./data/trips --bucket trips --prefix trips
python manage.py register_dataset trips s3://trips/trips
```

## Running the whole stack

`docker compose up` brings up MinIO, Postgres, Redis, the web server, and a Celery
worker. With Redis present, queries run on the worker and the page polls for live
status instead of executing inline.

```bash
docker compose up --build
# in another shell, once it's up:
docker compose exec web python manage.py register_dataset trips ./data/trips
```

Config is all environment variables (see `.env.example`). The rule is simple:
no `POSTGRES_HOST` means SQLite, no `REDIS_URL` means eager Celery. Same image,
laptop or server.

## Tests

```bash
pytest tests/ -q
# 35 passed
```

The suite builds a small partitioned dataset in a temp dir and checks the engine's
output against DuckDB reading the same files. DuckDB is the oracle for "what's the
right answer," so correctness tests are just `engine == duckdb` — differential
testing, not hand-written expected values.

| file | what it pins down |
|---|---|
| `test_parser.py` | SQL → logical plan; non-`SELECT` is rejected |
| `test_optimizer.py` | projection pushdown, predicate pushdown, partition/data predicate split |
| `test_executor.py` | pruning actually fires (N files skipped, N row-groups skipped, fewer bytes projected) |
| `test_nulls.py` | SQL NULL semantics survive the partial-aggregate → merge path |
| `test_e2e.py` | `run_sql` / `explain_sql`, and parallel results == serial results |
| `test_rendering.py` | a NULL in the UI reads `NULL`, not Python's `None` |

Two classes of bug the suite exists to catch, both of which it has caught:

- **Byte accounting.** `test_full_scan_reads_everything` asserts a query with
  nothing to prune reads *exactly* 100% — same unit on both sides of the ratio,
  pruned row-groups still in the denominator. That invariant found a bug where
  every query looked ~1.5x better than it was.
- **NULL semantics under parallelism.** `AVG`/`SUM` are reconstructed from
  partial sums and counts so workers can aggregate independently. A group whose
  every value is NULL gives `0/0`, and polars' `.sum()` returns `0` where SQL
  says NULL. The fixture guarantees an all-NULL group (`tip` is NULL for every
  wallet payment) and an all-NULL column, and the same query is run at
  `max_workers=1` and `4` to prove the merge doesn't change the answer.

## Benchmark

```bash
python scripts/benchmark.py --data ./data/trips --runs 7
# add Postgres to include the load-then-query baseline:
python scripts/benchmark.py --data ./data/trips \
  --postgres "host=localhost dbname=queryengine user=queryengine password=queryengine"
```

Here's a real run on the sample dataset (~300k rows, median of 7):

```
query             wall clock
-----------------------------
agg_by_region       68.5 ms
filtered_agg       127.3 ms
selective_scan      30.6 ms
partition_filter    26.7 ms
wide_group         109.6 ms
```

(`benchmark_results.json` in the repo root is the raw output of this exact run.)

Treat those as order-of-magnitude only: on an unquiet laptop the same invocation
varies by up to 2x run to run. The absolute numbers also move with the dataset —
these are from a 300k-row, 15-file, 120-row-group sample, so most of each
measurement is fixed overhead (footer reads, plan building, process setup)
rather than scan work.

The table that actually matters is what each query *read*. Unlike the timings,
these are deterministic, byte-identical across runs:

| query | bytes scanned | how |
|---|---|---|
| `COUNT(*) WHERE trip_id > 299990` | 14 KB / 5.90 MB (**0.2%**) | 119 of 120 row-groups skipped on min/max stats |
| `COUNT(*) WHERE trip_id > 250000` | 271 KB / 5.90 MB (**4.5%**) | 100 of 120 row-groups skipped |
| `SUM(price) WHERE region='EU'` | 577 KB / 5.97 MB (**9.4%**) | 10 of 15 files pruned by partition |
| `GROUP BY region, payment` | 106 KB / 5.90 MB (**1.8%**) | 1 of 7 columns read, and it's dictionary-encoded |
| `GROUP BY region` | 85 KB / 5.90 MB (**1.4%**) | groups on a partition column, so no data column is referenced |
| `COUNT(*)` | 85 KB / 5.90 MB (**1.4%**) | narrowest column in the file, read only to count rows |

The selective filter opens 1 row-group out of 120; the partition filter never
touches the US/APAC directories. The `GROUP BY` rows have nothing to push down,
so projection is the only lever and the ratio becomes "how wide is the column I
asked for" — `payment` is a low-cardinality string that compresses to almost
nothing.

The last two rows are a deliberate choice. `COUNT(*)` and `GROUP BY region`
reference no data column at all (`region` lives in the directory path), but a
row count still has to come from somewhere, so exactly one column gets read.
*Which* column is decided in physical planning from the footer's per-column
compressed sizes: reading the first declared column instead — a wide monotonic
`trip_id` — cost 1.59 MB for the identical answer, 19x more I/O. Pinned by
`test_count_star_reads_the_narrowest_column`.

The Postgres column (when you pass `--postgres`) counts the one-time COPY load
against it, which is the honest version of the "no loading step" pitch — that
load alone dwarfs every query above.

Both sides of every ratio are compressed on-disk bytes. An earlier version of
this table compared compressed `bytes_scanned` against *uncompressed*
`bytes_total` and excluded pruned row-groups from the denominator, which
flattered every number by roughly the compression ratio and hid row-group
pruning entirely. Fixing it made the projection-only queries look worse and the
pruning-heavy ones look dramatically better, which is the correct outcome: a
full scan now reports exactly 100%.

## Layout

```
apps/engine/        the engine, no Django imports anywhere
  parser.py         sqlglot -> logical plan
  predicates.py     predicate tree + row-group skip logic
  logical_plan.py   plan nodes + Mermaid
  optimizer.py      projection / predicate pushdown, partition split
  parquet_reader.py storage, file listing, footer stats (local + S3)
  physical_plan.py  build scan tasks + pruning metrics
  executor.py       process pool, partial aggregation, merge
  api.py            run_sql / explain_sql
apps/catalog/       Dataset / Column / Partition + schema introspection
apps/queries/       QueryRun + Celery task + Redis stats cache
apps/frontend/      htmx views and templates
config/             Django project + Celery
scripts/            seed_data, upload_minio, benchmark
tests/              parser, optimizer, executor, nulls, rendering, e2e
```

## Deploying it for free

See [docs/DEPLOY.md](docs/DEPLOY.md) for a step-by-step on a free Oracle Cloud
VM (and a Fly.io alternative). The whole thing fits on one small box.
