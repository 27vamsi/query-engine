"""Public engine entrypoint.

This is the only module the Django/Celery layer should import. It glues the
phases together:

    SQL --parse--> logical plan --optimize--> pushed-down plan
        --physical plan (prune)--> tasks --execute (parallel)--> rows

`run_sql` runs the whole pipeline. `explain_sql` stops after planning and
returns the optimized plan + a Mermaid diagram for the UI.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .logical_plan import LogicalNode, find_scan, format_plan, to_mermaid
from .optimizer import OptimizerContext, optimize
from .parquet_reader import (
    list_files,
    read_row_group_metadata,
    resolve_fs,
)
from .parser import parse_sql
from .physical_plan import build_physical_plan
from .executor import execute


@dataclass
class DatasetSchema:
    columns: list[str]
    partition_columns: list[str] = field(default_factory=list)


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[dict]
    metrics: dict
    plan_text: str
    plan_mermaid: str
    duration_ms: float


def infer_schema(dataset_uri: str) -> DatasetSchema:
    """Read one file's footer for data columns and parse Hive partition keys
    from its path. Cheap; only touches a single footer."""
    fs, base = resolve_fs(dataset_uri)
    files = list_files(fs, base)
    if not files:
        raise FileNotFoundError(f"no parquet files under {dataset_uri}")
    sample = files[0]
    groups = read_row_group_metadata(fs, sample)
    data_cols = list(groups[0].stats.keys()) if groups else []
    partition_cols = list(sample.partitions.keys())
    return DatasetSchema(columns=data_cols + partition_cols, partition_columns=partition_cols)


def plan_sql(
    sql: str, dataset_uri: str, schema: DatasetSchema | None = None
) -> tuple[LogicalNode, DatasetSchema]:
    schema = schema or infer_schema(dataset_uri)
    logical = parse_sql(sql)
    ctx = OptimizerContext(
        all_columns=schema.columns, partition_columns=schema.partition_columns
    )
    optimized = optimize(logical, ctx)
    return optimized, schema


def explain_sql(
    sql: str, dataset_uri: str, schema: DatasetSchema | None = None
) -> dict:
    plan, _ = plan_sql(sql, dataset_uri, schema)
    return {
        "plan_text": format_plan(plan),
        "plan_mermaid": to_mermaid(plan),
    }


def run_sql(
    sql: str,
    dataset_uri: str,
    schema: DatasetSchema | None = None,
    max_workers: int | None = None,
    metadata_fn=read_row_group_metadata,
) -> QueryResult:
    start = time.perf_counter()
    plan, schema = plan_sql(sql, dataset_uri, schema)
    scan = find_scan(plan)
    fs, base = resolve_fs(dataset_uri)

    physical = build_physical_plan(scan, fs, base, metadata_fn=metadata_fn)
    result = execute(
        plan, dataset_uri, physical.tasks, physical.metrics, max_workers=max_workers
    )

    duration_ms = (time.perf_counter() - start) * 1000
    return QueryResult(
        columns=result.columns,
        rows=result.rows,
        metrics=result.metrics.as_dict(),
        plan_text=format_plan(plan),
        plan_mermaid=to_mermaid(plan),
        duration_ms=round(duration_ms, 2),
    )
