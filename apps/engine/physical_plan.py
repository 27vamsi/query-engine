"""Physical planning: turn an optimized Scan into concrete units of work.

A "unit of work" is one file + the list of its row-group indices that survived
pruning. Splitting at row-group granularity is what lets us parallelise across a
process pool and is also where we compute the headline metrics:

    files_pruned        — whole files skipped via partition pruning
    row_groups_pruned   — row-groups skipped via min/max stats pushdown
    bytes_scanned       — compressed bytes of (projected columns x kept groups)
    bytes_total         — what a naive full scan would have read

The ratio bytes_scanned / bytes_total is the demo money shot.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pyarrow.fs as pafs

from .logical_plan import Scan
from .parquet_reader import (
    ParquetFileInfo,
    list_files,
    prune_files,
    read_row_group_metadata,
)
from .predicates import Predicate


@dataclass
class ScanTask:
    path: str
    row_groups: list[int]
    columns: list[str] | None
    predicate: Predicate | None  # rebuilt into an Arrow expression in the worker
    partitions: dict = field(default_factory=dict)  # Hive values for this file


@dataclass
class ScanMetrics:
    total_files: int = 0
    files_scanned: int = 0
    files_pruned: int = 0
    total_row_groups: int = 0
    row_groups_scanned: int = 0
    row_groups_pruned: int = 0
    bytes_scanned: int = 0
    bytes_total: int = 0
    rows_scanned: int = 0

    def as_dict(self) -> dict:
        return {
            "total_files": self.total_files,
            "files_scanned": self.files_scanned,
            "files_pruned": self.files_pruned,
            "total_row_groups": self.total_row_groups,
            "row_groups_scanned": self.row_groups_scanned,
            "row_groups_pruned": self.row_groups_pruned,
            "bytes_scanned": self.bytes_scanned,
            "bytes_total": self.bytes_total,
            "rows_scanned": self.rows_scanned,
        }


@dataclass
class PhysicalPlan:
    tasks: list[ScanTask] = field(default_factory=list)
    metrics: ScanMetrics = field(default_factory=ScanMetrics)


def build_physical_plan(
    scan: Scan,
    fs: pafs.FileSystem,
    base_path: str,
    metadata_fn=read_row_group_metadata,
) -> PhysicalPlan:
    """metadata_fn is injectable so the Django layer can supply a Redis-cached
    version without the engine knowing about Redis."""
    metrics = ScanMetrics()

    all_files = list_files(fs, base_path)
    metrics.total_files = len(all_files)

    kept_files, pruned = prune_files(all_files, scan.partition_filter)
    metrics.files_pruned = pruned
    metrics.files_scanned = len(kept_files)

    tasks: list[ScanTask] = []
    projection = scan.projection

    for file in kept_files:
        groups = metadata_fn(fs, file)
        metrics.total_row_groups += len(groups)
        kept_indices: list[int] = []
        for g in groups:
            if scan.pushed_filter is not None and scan.pushed_filter.can_skip_row_group(
                g.stats
            ):
                metrics.row_groups_pruned += 1
                continue
            kept_indices.append(g.index)
            metrics.row_groups_scanned += 1
            metrics.rows_scanned += g.num_rows
            metrics.bytes_scanned += _projected_bytes(g.column_byte_size, projection)
            metrics.bytes_total += g.total_byte_size
        if kept_indices:
            tasks.append(
                ScanTask(
                    path=file.path,
                    row_groups=kept_indices,
                    columns=projection,
                    predicate=scan.pushed_filter,
                    partitions=file.partitions,
                )
            )

    # account for pruned files' bytes in bytes_total so the ratio is honest
    metrics.bytes_total += _pruned_file_bytes(all_files, kept_files)

    return PhysicalPlan(tasks=tasks, metrics=metrics)


def _projected_bytes(col_bytes: dict[str, int], projection: list[str] | None) -> int:
    if projection is None:
        return sum(col_bytes.values())
    return sum(col_bytes.get(c, 0) for c in projection)


def _pruned_file_bytes(
    all_files: list[ParquetFileInfo], kept: list[ParquetFileInfo]
) -> int:
    kept_paths = {f.path for f in kept}
    return sum(f.size for f in all_files if f.path not in kept_paths)
