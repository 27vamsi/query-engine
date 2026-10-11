"""Shared fixtures.

`trips_dataset` builds a small Hive-partitioned Parquet tree in a temp dir with
multiple row-groups per file (so row-group pruning is exercisable) and yields
its URI plus a DuckDB oracle connection over the same files.
"""

from __future__ import annotations

import math
import os
import random

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

REGIONS = ["EU", "US", "APAC"]
PAYMENT = ["cash", "card", "wallet"]


def _write_partition(path: str, n: int, base: int, seed: int) -> None:
    rng = random.Random(seed)
    distance = [round(rng.uniform(0.3, 40.0), 2) for _ in range(n)]
    price = [round(d * rng.uniform(2.0, 4.5) + rng.uniform(2, 6), 2) for d in distance]
    payment = [rng.choice(PAYMENT) for _ in range(n)]

    # NULLs are where analytical engines actually diverge, so the oracle has to
    # see them. `tip` is always NULL for wallet payments, which guarantees a
    # group whose every value is NULL -- the case that makes a sum/count
    # reconstruction of AVG divide 0 by 0.
    tip = [
        None if p == "wallet" or rng.random() < 0.1 else round(pr * rng.uniform(0, 0.3), 2)
        for p, pr in zip(payment, price)
    ]
    # a NULL group-by key, and a column that is NULL everywhere
    payment_with_nulls = [None if rng.random() < 0.05 else p for p in payment]
    table = pa.table(
        {
            "trip_id": pa.array(range(base, base + n), pa.int64()),
            "passengers": pa.array([rng.randint(1, 4) for _ in range(n)], pa.int32()),
            "distance": pa.array(distance, pa.float64()),
            "price": pa.array(price, pa.float64()),
            "tip": pa.array(tip, pa.float64()),
            "payment": pa.array(payment_with_nulls, pa.string()),
            "surcharge": pa.array([None] * n, pa.float64()),
        }
    )
    os.makedirs(os.path.dirname(path), exist_ok=True)
    pq.write_table(table, path, row_group_size=max(1, n // 4))


@pytest.fixture(scope="session")
def trips_dataset(tmp_path_factory):
    root = tmp_path_factory.mktemp("trips")
    dates = ["2024-01-01", "2024-01-02", "2024-01-03"]
    base = 0
    per = 4000
    for i, region in enumerate(REGIONS):
        for j, date in enumerate(dates):
            path = os.path.join(str(root), f"region={region}", f"date={date}", "part.parquet")
            _write_partition(path, per, base, seed=i * 10 + j)
            base += per
    return str(root)


@pytest.fixture(scope="session")
def oracle(trips_dataset):
    con = duckdb.connect()
    glob = os.path.join(trips_dataset, "**", "*.parquet").replace("\\", "/")
    con.execute(
        f"CREATE VIEW trips AS SELECT * FROM read_parquet('{glob}', hive_partitioning=true)"
    )
    return con


def assert_matches(engine_result, oracle_con, sql: str):
    """Compare engine rows to DuckDB rows (order-insensitive, float-rounded)."""
    orc = oracle_con.execute(sql).fetchall()

    def norm(v):
        if v is None:
            return None
        if isinstance(v, float):
            # NaN has to stay distinguishable from NULL: returning NaN where SQL
            # says NULL is exactly the kind of divergence this oracle exists to
            # catch, and NaN != NaN would otherwise fail for the wrong reason.
            if math.isnan(v):
                return "NaN"
            return round(v, 3)
        # DuckDB returns hive date partitions as date objects; compare as str
        return str(v) if not isinstance(v, (int, str)) else v

    # Sort by repr: a column holding both NULL and floats is not orderable, but
    # repr gives a deterministic total order, which is all a multiset comparison
    # needs.
    eng_rows = sorted(
        (tuple(norm(row[c]) for c in engine_result.columns) for row in engine_result.rows),
        key=repr,
    )
    orc_rows = sorted((tuple(norm(v) for v in r) for r in orc), key=repr)
    assert eng_rows == orc_rows, f"\nengine={eng_rows[:3]}\noracle={orc_rows[:3]}"
