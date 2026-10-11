"""Parallel execution: map-reduce over row-groups.

Flow:
    map     each ScanTask -> a worker process reads its row-groups (column
            pruned + filtered) and computes a PARTIAL aggregate (or returns the
            filtered batch for non-aggregate queries).
    reduce  the coordinator concatenates partials and finishes the aggregation
            (e.g. avg = sum(partial_sums) / sum(partial_counts)), then applies
            the final Project / Sort / Limit.

Why partial aggregation matters: each worker ships back a tiny grouped frame
instead of millions of rows, so the cross-process transfer stays cheap. That is
the same shape as a distributed shuffle, just on one box.
"""

from __future__ import annotations

import multiprocessing as mp
import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass

import polars as pl

from .logical_plan import (
    Aggregate,
    AggregateExpr,
    Limit,
    LogicalNode,
    Project,
    Sort,
    walk,
)
from .parquet_reader import read_row_groups, resolve_fs
from .physical_plan import ScanMetrics, ScanTask


@dataclass
class ExecResult:
    columns: list[str]
    rows: list[dict]
    metrics: ScanMetrics


# --------------------------------------------------------------------------
# Worker (runs in a separate process). Must be top-level + picklable args.
# --------------------------------------------------------------------------


def _read_task(dataset_uri: str, task: ScanTask) -> pl.DataFrame:
    fs, _ = resolve_fs(dataset_uri)
    arrow_filter = task.predicate.to_arrow() if task.predicate is not None else None
    table = read_row_groups(fs, task.path, task.row_groups, task.columns, arrow_filter)
    df = pl.from_arrow(table)
    if not isinstance(df, pl.DataFrame):
        df = pl.DataFrame(df)
    # Partition columns live in the path, not the row data. Materialise them as
    # constant columns so GROUP BY / SELECT / ORDER BY on them just works.
    for name, value in task.partitions.items():
        if name not in df.columns:
            df = df.with_columns(pl.lit(value).alias(name))
    return df


def _worker_aggregate(args) -> bytes:
    dataset_uri, task, group_by, agg_specs = args
    df = _read_task(dataset_uri, task)
    partial = _partial_aggregate(df, group_by, agg_specs)
    return partial.serialize(format="binary")


def _worker_scan(args) -> bytes:
    dataset_uri, task = args
    df = _read_task(dataset_uri, task)
    return df.serialize(format="binary")


def _partial_aggregate(
    df: pl.DataFrame, group_by: list[str], agg_specs: list[tuple]
) -> pl.DataFrame:
    exprs = []
    for func, column, output in agg_specs:
        if func == "count" and column is None:
            exprs.append(pl.len().alias(f"{output}__cnt"))
        elif func == "count":
            exprs.append(pl.col(column).count().alias(f"{output}__cnt"))
        elif func == "sum":
            # The companion count distinguishes "summed nothing" (SQL: NULL)
            # from "summed to zero" -- polars returns 0 for both.
            exprs.append(pl.col(column).sum().alias(f"{output}__sum"))
            exprs.append(pl.col(column).count().alias(f"{output}__sumcnt"))
        elif func == "min":
            exprs.append(pl.col(column).min().alias(f"{output}__min"))
        elif func == "max":
            exprs.append(pl.col(column).max().alias(f"{output}__max"))
        elif func == "avg":
            exprs.append(pl.col(column).sum().alias(f"{output}__avgsum"))
            exprs.append(pl.col(column).count().alias(f"{output}__avgcnt"))
    if group_by:
        return df.group_by(group_by).agg(exprs)
    return df.select(exprs)


def _merge_aggregate(
    partials: list[pl.DataFrame],
    group_by: list[str],
    aggs: list[AggregateExpr],
    output_columns: list[str],
) -> pl.DataFrame:
    if not partials:
        combined = pl.DataFrame()
    else:
        combined = pl.concat(partials, how="vertical_relaxed")

    merge_exprs = []
    for a in aggs:
        o = a.output
        if a.func == "count":
            merge_exprs.append(pl.col(f"{o}__cnt").sum().alias(o))
        elif a.func == "sum":
            non_null = pl.col(f"{o}__sumcnt").sum()
            merge_exprs.append(
                pl.when(non_null > 0)
                .then(pl.col(f"{o}__sum").sum())
                .otherwise(None)
                .alias(o)
            )
        elif a.func == "min":
            merge_exprs.append(pl.col(f"{o}__min").min().alias(o))
        elif a.func == "max":
            merge_exprs.append(pl.col(f"{o}__max").max().alias(o))
        elif a.func == "avg":
            # SQL says AVG over no non-null values is NULL, not 0/0. Without the
            # guard the sum/count reconstruction yields NaN for a group whose
            # every value was NULL, which silently diverges from every real
            # engine.
            non_null = pl.col(f"{o}__avgcnt").sum()
            merge_exprs.append(
                pl.when(non_null > 0)
                .then(pl.col(f"{o}__avgsum").sum() / non_null)
                .otherwise(None)
                .alias(o)
            )

    if combined.is_empty():
        # Produce an empty frame with the right columns.
        return pl.DataFrame({c: [] for c in output_columns})

    if group_by:
        result = combined.group_by(group_by).agg(merge_exprs)
    else:
        result = combined.select(merge_exprs)

    present = [c for c in output_columns if c in result.columns]
    return result.select(present)


# --------------------------------------------------------------------------
# Coordinator
# --------------------------------------------------------------------------


def execute(
    plan: LogicalNode,
    dataset_uri: str,
    tasks: list[ScanTask],
    metrics: ScanMetrics,
    max_workers: int | None = None,
) -> ExecResult:
    workers = _decide_workers(max_workers, metrics, len(tasks))

    agg = _find(plan, Aggregate)
    if agg is not None:
        df = _run_aggregate(agg, dataset_uri, tasks, workers)
    else:
        df = _run_scan(dataset_uri, tasks, workers)
        df = _apply_projection(plan, df)

    df = _apply_sort(plan, df)
    df = _apply_limit(plan, df)

    rows = df.to_dicts()
    return ExecResult(columns=df.columns, rows=rows, metrics=metrics)


# Below this many scanned rows, spinning up worker processes costs more than it
# saves (especially on spawn-based platforms like Windows/macOS), so run serial.
PARALLEL_ROW_THRESHOLD = 2_000_000


def _decide_workers(max_workers: int | None, metrics: ScanMetrics, n_tasks: int) -> int:
    if max_workers is not None:
        return max(1, min(max_workers, n_tasks))
    if n_tasks <= 1 or metrics.rows_scanned < PARALLEL_ROW_THRESHOLD:
        return 1
    return min(n_tasks, os.cpu_count() or 1)


def _run_aggregate(
    agg: Aggregate, dataset_uri: str, tasks: list[ScanTask], max_workers: int | None
) -> pl.DataFrame:
    agg_specs = [(a.func, a.column, a.output) for a in agg.aggregates]
    args = [(dataset_uri, t, agg.group_by, agg_specs) for t in tasks]

    partials_bytes = _map(_worker_aggregate, args, max_workers)
    partials = [pl.DataFrame.deserialize(_buf(b), format="binary") for b in partials_bytes]
    output_cols = agg.output_columns or (agg.group_by + [a.output for a in agg.aggregates])
    return _merge_aggregate(partials, agg.group_by, agg.aggregates, output_cols)


def _run_scan(
    dataset_uri: str, tasks: list[ScanTask], max_workers: int | None
) -> pl.DataFrame:
    args = [(dataset_uri, t) for t in tasks]
    bufs = _map(_worker_scan, args, max_workers)
    frames = [pl.DataFrame.deserialize(_buf(b), format="binary") for b in bufs]
    frames = [f for f in frames if f.height or f.width]
    if not frames:
        return pl.DataFrame()
    return pl.concat(frames, how="vertical_relaxed")


def _map(worker, args, max_workers):
    """Run worker over args, in a process pool unless there is trivially little
    work (or pools are unavailable, e.g. some sandboxes)."""
    if not args:
        return []
    workers = max_workers or min(len(args), os.cpu_count() or 1)
    if workers <= 1 or len(args) == 1:
        return [worker(a) for a in args]
    try:
        with ProcessPoolExecutor(max_workers=workers, mp_context=_SPAWN) as pool:
            return list(pool.map(worker, args))
    except Exception:
        # Fall back to serial execution if the pool cannot start.
        return [worker(a) for a in args]


# "spawn", not Linux's default "fork". polars keeps a rayon thread pool alive in
# this process, and fork() copies its mutexes in whatever state they happened to
# be in -- the child then blocks forever on a lock no thread exists to release.
# That deadlock is silent (no exception, so the fallback above never fires) and
# only shows up once something has warmed the thread pool, which is why the
# first few queries in a batch succeed and a later one hangs. spawn starts the
# child from a clean interpreter, so there are no inherited locks to wait on.
# Windows and macOS already default to spawn, so this only changes Linux.
_SPAWN = mp.get_context("spawn")


def _buf(b: bytes):
    import io

    return io.BytesIO(b)


# --------------------------------------------------------------------------
# Final operators
# --------------------------------------------------------------------------


def _apply_projection(plan: LogicalNode, df: pl.DataFrame) -> pl.DataFrame:
    proj = _find(plan, Project)
    if proj is None or df.is_empty():
        return df
    exprs = []
    for name, src in proj.columns:
        if src == name and src in df.columns:
            exprs.append(pl.col(src))
        elif src in df.columns:
            exprs.append(pl.col(src).alias(name))
        else:
            # scalar expression like price * qty
            exprs.append(pl.sql_expr(src).alias(name))
    return df.select(exprs)


def _apply_sort(plan: LogicalNode, df: pl.DataFrame) -> pl.DataFrame:
    s = _find(plan, Sort)
    if s is None or df.is_empty():
        return df
    cols = [c for c, _ in s.keys if c in df.columns]
    desc = [d for c, d in s.keys if c in df.columns]
    if not cols:
        return df
    return df.sort(cols, descending=desc)


def _apply_limit(plan: LogicalNode, df: pl.DataFrame) -> pl.DataFrame:
    lim = _find(plan, Limit)
    if lim is None:
        return df
    return df.head(lim.n)


def _find(plan: LogicalNode, cls):
    for n in walk(plan):
        if isinstance(n, cls):
            return n
    return None
