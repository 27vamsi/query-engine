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
    columns_projected: int = 0
    columns_total: int = 0

    # Where the bytes we did NOT read went. These three plus bytes_scanned sum
    # to exactly bytes_total, which is what makes the UI waterfall add up (and
    # is asserted in the tests).
    bytes_pruned_files: int = 0
    bytes_pruned_row_groups: int = 0
    bytes_unprojected: int = 0

    # Per-file / per-row-group outcome, for the UI heatmap. Pruned files have
    # no `groups` at all: their footers were never read, which is the point.
    scan_map: list[dict] = field(default_factory=list)

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
            "columns_projected": self.columns_projected,
            "columns_total": self.columns_total,
            "bytes_pruned_files": self.bytes_pruned_files,
            "bytes_pruned_row_groups": self.bytes_pruned_row_groups,
            "bytes_unprojected": self.bytes_unprojected,
            "scan_map": self.scan_map,
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

    kept_paths = {f.path for f in kept_files}

    for file in all_files:
        if file.path not in kept_paths:
            # Partition-pruned: we never read this footer, so we genuinely do
            # not know how many row-groups it holds. The UI shows it as one
            # opaque tile rather than inventing a count.
            metrics.scan_map.append({"name": _label(file), "pruned": True, "groups": []})
            continue

        groups = metadata_fn(fs, file)
        metrics.total_row_groups += len(groups)
        if groups and projection == []:
            # The optimizer found no referenced data column, but a row count
            # has to come from somewhere. Pick the narrowest column on disk --
            # the first declared column is usually a wide monotonic id, and
            # reading it instead of a dictionary-encoded string costs an order
            # of magnitude more I/O for the same answer. Resolved once, from
            # the first footer, so every file and the metrics agree.
            projection = _narrowest_column(groups[0].column_byte_size)
            scan.projection = projection
        if groups and not metrics.columns_total:
            data_columns = list(groups[0].column_byte_size)
            metrics.columns_total = len(data_columns)
            metrics.columns_projected = (
                len(data_columns) if projection is None else len(set(projection) & set(data_columns))
            )
        kept_indices: list[int] = []
        read_flags: list[bool] = []
        for g in groups:
            # Every row-group counts toward bytes_total, including the ones we
            # skip: the denominator is "what a naive full scan would have read",
            # so pruning has to be visible in the ratio. Both sides of the ratio
            # are compressed on-disk bytes -- comparing a projected compressed
            # read against an uncompressed total would flatter every query by
            # the compression ratio.
            group_bytes = sum(g.column_byte_size.values())
            metrics.bytes_total += group_bytes
            if scan.pushed_filter is not None and scan.pushed_filter.can_skip_row_group(
                g.stats
            ):
                metrics.row_groups_pruned += 1
                metrics.bytes_pruned_row_groups += group_bytes
                read_flags.append(False)
                continue
            kept_indices.append(g.index)
            read_flags.append(True)
            metrics.row_groups_scanned += 1
            metrics.rows_scanned += g.num_rows
            projected = _projected_bytes(g.column_byte_size, projection)
            metrics.bytes_scanned += projected
            metrics.bytes_unprojected += group_bytes - projected
        metrics.scan_map.append(
            {"name": _label(file), "pruned": False, "groups": read_flags}
        )
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
    metrics.bytes_pruned_files = _pruned_file_bytes(all_files, kept_files)
    metrics.bytes_total += metrics.bytes_pruned_files

    return PhysicalPlan(tasks=tasks, metrics=metrics)


def _label(file: ParquetFileInfo) -> str:
    """A short human label for the heatmap: the Hive partition it came from."""
    if file.partitions:
        return "/".join(f"{k}={v}" for k, v in file.partitions.items())
    return file.path.rsplit("/", 1)[-1]


def _narrowest_column(col_bytes: dict[str, int]) -> list[str]:
    if not col_bytes:
        return []
    return [min(col_bytes, key=lambda c: col_bytes[c])]


def _projected_bytes(col_bytes: dict[str, int], projection: list[str] | None) -> int:
    if projection is None:
        return sum(col_bytes.values())
    return sum(col_bytes.get(c, 0) for c in projection)


def _pruned_file_bytes(
    all_files: list[ParquetFileInfo], kept: list[ParquetFileInfo]
) -> int:
    kept_paths = {f.path for f in kept}
    return sum(f.size for f in all_files if f.path not in kept_paths)
