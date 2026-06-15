"""Generate a Hive-partitioned Parquet dataset that looks like NYC taxi trips.

Usage:
    python scripts/seed_data.py --out ./data/trips --rows 2000000

Layout:
    <out>/region=<R>/date=<YYYY-MM-DD>/part.parquet

Writing multiple row-groups per file (via row_group_size) is what makes
row-group pruning observable. Upload the same tree to MinIO/S3 with `mc` or the
boto helper in scripts/upload_minio.py and the engine reads it unchanged.
"""

from __future__ import annotations

import argparse
import os
import random

import pyarrow as pa
import pyarrow.parquet as pq

REGIONS = ["EU", "US", "APAC"]
PAYMENT = ["cash", "card", "wallet"]


def gen_partition(path: str, n: int, base_passenger: int, seed: int) -> None:
    rng = random.Random(seed)
    pickup_hour = [rng.randint(0, 23) for _ in range(n)]
    passengers = [rng.randint(1, 4) for _ in range(n)]
    distance = [round(rng.uniform(0.3, 40.0), 2) for _ in range(n)]
    # price correlated with distance, so min/max stats are meaningful
    price = [round(d * rng.uniform(2.0, 4.5) + rng.uniform(2, 6), 2) for d in distance]
    tip = [round(p * rng.uniform(0.0, 0.3), 2) for p in price]
    payment = [rng.choice(PAYMENT) for _ in range(n)]

    table = pa.table(
        {
            "trip_id": pa.array(range(base_passenger, base_passenger + n), pa.int64()),
            "pickup_hour": pa.array(pickup_hour, pa.int32()),
            "passengers": pa.array(passengers, pa.int32()),
            "distance": pa.array(distance, pa.float64()),
            "price": pa.array(price, pa.float64()),
            "tip": pa.array(tip, pa.float64()),
            "payment": pa.array(payment, pa.string()),
        }
    )
    os.makedirs(os.path.dirname(path), exist_ok=True)
    pq.write_table(table, path, row_group_size=max(1, n // 8), compression="snappy")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="./data/trips")
    ap.add_argument("--rows", type=int, default=2_000_000)
    ap.add_argument("--days", type=int, default=10)
    args = ap.parse_args()

    dates = [f"2024-01-{d:02d}" for d in range(1, args.days + 1)]
    partitions = [(r, d) for r in REGIONS for d in dates]
    per_part = max(1, args.rows // len(partitions))

    base = 0
    for i, (region, date) in enumerate(partitions):
        path = os.path.join(args.out, f"region={region}", f"date={date}", "part.parquet")
        gen_partition(path, per_part, base, seed=i)
        base += per_part
        print(f"wrote {path} ({per_part} rows)")

    print(f"\nDone. {len(partitions)} files, ~{base} rows under {args.out}")


if __name__ == "__main__":
    main()
