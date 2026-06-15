"""Render benchmark_results.json into a chart for the slide deck.

    python scripts/plot_benchmark.py
    python scripts/plot_benchmark.py --in benchmark_results.json --out docs/benchmark.png

Two panels: query latency (engine vs duckdb) and the bytes-scanned story, which
is the number the project is actually about.
"""

from __future__ import annotations

import argparse
import json

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


def load(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default="benchmark_results.json")
    ap.add_argument("--out", default="docs/benchmark.png")
    args = ap.parse_args()

    data = load(args.inp)
    engine = data["engine"]
    names = [n for n in engine if not n.startswith("_")]
    labels = [n.replace("_", "\n") for n in names]
    x = np.arange(len(names))

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 5.2))
    fig.suptitle(
        "query-engine over Parquet  (300k rows, median of 5 runs)",
        fontsize=14,
        fontweight="bold",
    )

    # Panel 1: latency, engine vs duckdb
    eng_ms = [engine[n]["ms"] for n in names]
    duck_ms = [data.get("duckdb", {}).get(n, {}).get("ms", 0) for n in names]
    pg = data.get("postgres", {})
    has_pg = bool(pg)
    width = 0.27 if has_pg else 0.38

    ax1.bar(x - width, eng_ms, width, label="engine", color="#4C72B0")
    ax1.bar(x, duck_ms, width, label="duckdb", color="#55A868")
    if has_pg:
        pg_ms = [pg.get(n, {}).get("ms", 0) for n in names]
        ax1.bar(x + width, pg_ms, width, label="postgres (query)", color="#C44E52")

    ax1.set_ylabel("latency (ms, lower is better)")
    ax1.set_title("Query latency")
    ax1.set_xticks(x)
    ax1.set_xticklabels(labels, fontsize=9)
    ax1.legend()
    ax1.grid(axis="y", alpha=0.3)
    for i, v in enumerate(eng_ms):
        ax1.text(i - width, v, f"{v:g}", ha="center", va="bottom", fontsize=8)
    for i, v in enumerate(duck_ms):
        ax1.text(i, v, f"{v:g}", ha="center", va="bottom", fontsize=8)

    # Panel 2: bytes scanned vs total -- the pruning story
    scanned = [engine[n]["metrics"]["bytes_scanned"] / 1e6 for n in names]
    total = [engine[n]["metrics"]["bytes_total"] / 1e6 for n in names]
    pct = [100 * s / t for s, t in zip(scanned, total)]

    ax2.bar(x, total, 0.55, label="total bytes", color="#DDDDDD", edgecolor="#999999")
    ax2.bar(x, scanned, 0.55, label="bytes actually read", color="#4C72B0")
    ax2.set_ylabel("megabytes")
    ax2.set_title("Data read vs data skipped (the columnar win)")
    ax2.set_xticks(x)
    ax2.set_xticklabels(labels, fontsize=9)
    ax2.legend()
    ax2.grid(axis="y", alpha=0.3)
    for i, (s, p) in enumerate(zip(scanned, pct)):
        ax2.text(i, s, f"{p:.0f}%", ha="center", va="bottom", fontsize=9, fontweight="bold")

    fig.tight_layout(rect=(0, 0, 1, 0.95))
    fig.savefig(args.out, dpi=150)
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
