"""Benchmark the engine against DuckDB and (optionally) Postgres.

The headline comparison the project promises: answering analytical queries over
Parquet directly vs loading the same data into Postgres first and querying it.

    python scripts/benchmark.py --data ./data/trips --runs 3
    python scripts/benchmark.py --data ./data/trips --postgres "host=localhost dbname=queryengine user=queryengine password=queryengine"

DuckDB is always included (it is the correctness oracle too). Postgres is
included only if --postgres is given; its timing includes the one-time load so
the comparison is honest about the "no loading step" claim.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apps.engine.api import run_sql

QUERIES = {
    "agg_by_region": "SELECT region, COUNT(*) AS n, SUM(price) AS rev, AVG(distance) AS d "
    "FROM trips GROUP BY region ORDER BY rev DESC",
    "filtered_agg": "SELECT payment, AVG(tip) AS t FROM trips WHERE price > 50 GROUP BY payment",
    "selective_scan": "SELECT COUNT(*) AS n FROM trips WHERE trip_id > 250000",
    "partition_filter": "SELECT region, SUM(price) AS rev FROM trips "
    "WHERE region = 'EU' GROUP BY region",
    "wide_group": "SELECT region, payment, COUNT(*) AS n, MAX(price) AS hi "
    "FROM trips GROUP BY region, payment",
}


def time_it(fn, runs: int) -> float:
    times = []
    for _ in range(runs):
        t0 = time.perf_counter()
        fn()
        times.append((time.perf_counter() - t0) * 1000)
    return round(statistics.median(times), 1)


def bench_engine(data: str, runs: int) -> dict:
    out = {}
    for name, sql in QUERIES.items():
        last = {}

        def go():
            nonlocal last
            r = run_sql(sql, data)  # let the engine auto-decide parallelism
            last = r.metrics

        out[name] = {"ms": time_it(go, runs), "metrics": last}
    return out


def bench_duckdb(data: str, runs: int) -> dict:
    import duckdb

    pattern = glob.glob(f"{data}/**/*.parquet", recursive=True)[0]
    g = f"{data}/**/*.parquet".replace("\\", "/")
    out = {}
    for name, sql in QUERIES.items():
        con = duckdb.connect()
        con.execute(
            f"CREATE VIEW trips AS SELECT * FROM read_parquet('{g}', hive_partitioning=true)"
        )
        out[name] = {"ms": time_it(lambda: con.execute(sql).fetchall(), runs)}
    return out


def bench_postgres(data: str, dsn: str, runs: int) -> dict:
    import psycopg
    import pyarrow.parquet as pq

    files = glob.glob(f"{data}/**/*.parquet", recursive=True)
    out = {}
    with psycopg.connect(dsn) as conn:
        # one-time load (counts against Postgres, by design)
        t0 = time.perf_counter()
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS bench_trips")
            cur.execute(
                "CREATE TABLE bench_trips (trip_id bigint, passengers int, distance double precision,"
                " price double precision, tip double precision, payment text, region text, date text)"
            )
            import re

            for f in files:
                parts = dict(re.findall(r"([^/\\=]+)=([^/\\]+)", f))
                table = pq.read_table(f)
                cols = table.to_pydict()
                rows = zip(*[cols[c] for c in cols])
                with cur.copy(
                    "COPY bench_trips (trip_id,passengers,distance,price,tip,payment,region,date) FROM STDIN"
                ) as cp:
                    for r in rows:
                        cp.write_row(list(r) + [parts.get("region"), parts.get("date")])
            cur.execute("ANALYZE bench_trips")
        conn.commit()
        load_ms = round((time.perf_counter() - t0) * 1000, 1)

        for name, sql in QUERIES.items():
            pg_sql = sql.replace("FROM trips", "FROM bench_trips")
            with conn.cursor() as cur:
                out[name] = {"ms": time_it(lambda: cur.execute(pg_sql) or cur.fetchall(), runs)}
        out["_load"] = {"ms": load_ms}
    return out


def render(results: dict) -> None:
    names = list(QUERIES)
    engines = list(results)
    width = max(len(n) for n in names) + 2
    header = "query".ljust(width) + "".join(e.ljust(14) for e in engines)
    print("\n" + header)
    print("-" * len(header))
    for n in names:
        row = n.ljust(width)
        for e in engines:
            ms = results[e].get(n, {}).get("ms", "-")
            row += f"{ms} ms".ljust(14)
        print(row)
    if "postgres" in results and "_load" in results["postgres"]:
        print(f"\n(postgres one-time load: {results['postgres']['_load']['ms']} ms)")

    print("\nEngine pruning metrics:")
    for n in names:
        m = results.get("engine", {}).get(n, {}).get("metrics", {})
        if m:
            print(
                f"  {n}: scanned {m.get('bytes_scanned')}/{m.get('bytes_total')} bytes, "
                f"files_pruned={m.get('files_pruned')}, rg_pruned={m.get('row_groups_pruned')}"
            )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="./data/trips")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--postgres", default="")
    ap.add_argument("--out", default="benchmark_results.json")
    args = ap.parse_args()

    results = {
        "engine": bench_engine(args.data, args.runs),
        "duckdb": bench_duckdb(args.data, args.runs),
    }
    if args.postgres:
        results["postgres"] = bench_postgres(args.data, args.postgres, args.runs)

    render(results)
    with open(args.out, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
