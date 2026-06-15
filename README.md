# query-engine

A small SQL engine that runs analytical queries straight over Parquet files in a
bucket. There's no load step. You point it at a prefix of Parquet, hand it SQL,
and it parses the query, builds a plan, throws away the partitions and columns it
doesn't need, pushes the `WHERE` clause down to the Parquet reader, fans the scan
out across a process pool, and merges the partial results.

It's basically a teaching-grade DuckDB/Presto. The interesting part isn't that it
returns the right answer (DuckDB does that faster). It's that you can see *why*
it's fast: how many files got skipped, how many row-groups got pruned, and how
few bytes actually came off disk.

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

Open http://localhost:8000. There's a `trips` dataset waiting; click into it, edit
the sample SQL, hit Execute, and you'll get the plan diagram plus the metrics.

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
- `bytes_scanned` vs `bytes_total` (column pruning + row-group pruning)
- the optimized plan as text and as a Mermaid diagram

`bytes_scanned / bytes_total` is the number to watch. On the sample data a
selective filter like `trip_id > 299990` reads a single row-group out of 120, and
a `region = 'EU'` query throws away two thirds of the files before opening
anything.

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
```

The suite builds a small partitioned dataset in a temp dir and checks the engine's
output against DuckDB reading the same files. DuckDB is the oracle for "what's the
right answer," so correctness tests are just `engine == duckdb`. There are also
direct asserts that pruning actually fires (N files skipped, N row-groups skipped,
fewer bytes for a projected query).

## Benchmark

```bash
python scripts/benchmark.py --data ./data/trips --runs 3
# add Postgres to include the load-then-query baseline:
python scripts/benchmark.py --data ./data/trips \
  --postgres "host=localhost dbname=queryengine user=queryengine password=queryengine"
```

Here's a real run on the sample dataset (~300k rows, median of 5):

```
query             engine        duckdb
----------------------------------------------
agg_by_region     32.5 ms       10.9 ms
filtered_agg      57.2 ms       14.6 ms
selective_scan    13.6 ms        6.6 ms
partition_filter  11.5 ms        6.8 ms
wide_group        50.7 ms       14.5 ms
```

DuckDB wins on raw speed and that's expected, it's a tuned C++ engine and this is
Python driving PyArrow/Polars. The table that actually matters is what each query
*read*:

| query | bytes scanned | how |
|---|---|---|
| `COUNT(*) WHERE trip_id > 250000` | 277 KB / 1.67 MB (**~17%**) | 100 row-groups skipped on min/max stats |
| `SUM(price) WHERE region='EU'` | 591 KB / 7.76 MB (**~8%**) | 10 of 15 files pruned by partition |
| `GROUP BY region` | 3.2 MB / 10 MB (**~32%**) | only the two referenced columns read |
| `GROUP BY region, payment` | 1.86 MB / 10 MB (**~19%**) | column projection |

The selective filter opens 1 row-group out of 120, the partition filter never
touches the US/APAC directories. The Postgres column (when you pass `--postgres`)
counts the one-time COPY load against it, which is the honest version of the
"no loading step" pitch, that load alone dwarfs every query above.

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
tests/              parser, optimizer, executor, e2e
```

## Deploying it for free

See [docs/DEPLOY.md](docs/DEPLOY.md) for a step-by-step on a free Oracle Cloud
VM (and a Fly.io alternative). The whole thing fits on one small box.
