"""PyArrow-backed Parquet access layer.

Everything that touches storage lives here. It works identically against a
local directory and against S3/MinIO, because PyArrow's filesystem abstraction
gives us one code path. The rest of the engine never imports pyarrow.fs.

Responsibilities:
  * resolve a dataset URI to (filesystem, base_path)
  * list parquet files, parsing Hive-style partition values from the path
  * prune whole files using a partition predicate (no I/O)
  * expose per-row-group column statistics (min/max) for row-group pruning
  * read selected columns of selected row-groups, applying the filter kernel
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.fs as pafs
import pyarrow.parquet as pq

from .predicates import ColumnStats, Comparison, In, Predicate

_HIVE_RE = re.compile(r"([^/=]+)=([^/]+)")


@dataclass
class ParquetFileInfo:
    path: str  # path within the filesystem
    size: int
    partitions: dict[str, object] = field(default_factory=dict)


@dataclass
class RowGroupInfo:
    file: ParquetFileInfo
    index: int
    num_rows: int
    total_byte_size: int  # compressed size of all column chunks
    column_byte_size: dict[str, int] = field(default_factory=dict)
    stats: dict[str, ColumnStats] = field(default_factory=dict)


def resolve_fs(uri: str) -> tuple[pafs.FileSystem, str]:
    """Return (filesystem, base_path) for a dataset URI.

    Supports:
      * local paths and file:// URIs
      * s3:// URIs (MinIO via env: AWS_ENDPOINT_URL, AWS_ACCESS_KEY_ID, ...)
    """
    parsed = urlparse(uri)
    # A single-letter scheme is a Windows drive letter (e.g. C:\...), not a URI.
    is_windows_drive = len(parsed.scheme) == 1
    if parsed.scheme in ("", "file") or is_windows_drive:
        path = parsed.path if parsed.scheme == "file" else uri
        return pafs.LocalFileSystem(), os.path.abspath(path)

    if parsed.scheme == "s3":
        endpoint = os.environ.get("AWS_ENDPOINT_URL")
        kwargs: dict[str, object] = {}
        if endpoint:
            kwargs["endpoint_override"] = endpoint
            kwargs["scheme"] = "https" if endpoint.startswith("https") else "http"
        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        if region:
            kwargs["region"] = region
        if os.environ.get("AWS_ACCESS_KEY_ID"):
            kwargs["access_key"] = os.environ["AWS_ACCESS_KEY_ID"]
            kwargs["secret_key"] = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
        fs = pafs.S3FileSystem(**kwargs)
        base = f"{parsed.netloc}{parsed.path}"
        return fs, base.rstrip("/")

    raise ValueError(f"unsupported dataset scheme: {parsed.scheme}")


def list_files(fs: pafs.FileSystem, base_path: str) -> list[ParquetFileInfo]:
    """Recursively list .parquet files under base_path, parsing partitions."""
    selector = pafs.FileSelector(base_path, recursive=True, allow_not_found=True)
    out: list[ParquetFileInfo] = []
    for info in fs.get_file_info(selector):
        if info.type != pafs.FileType.File:
            continue
        if not info.path.endswith(".parquet"):
            continue
        rel = info.path[len(base_path):].lstrip("/")
        partitions = dict(_HIVE_RE.findall(os.path.dirname(rel)))
        out.append(
            ParquetFileInfo(
                path=info.path,
                size=info.size or 0,
                partitions=_coerce_partition_types(partitions),
            )
        )
    return out


def _coerce_partition_types(parts: dict[str, str]) -> dict[str, object]:
    out: dict[str, object] = {}
    for k, v in parts.items():
        out[k] = _coerce_scalar(v)
    return out


def _coerce_scalar(v: str) -> object:
    try:
        return int(v)
    except ValueError:
        try:
            return float(v)
        except ValueError:
            return v


def prune_files(
    files: list[ParquetFileInfo], partition_filter: Predicate | None
) -> tuple[list[ParquetFileInfo], int]:
    """Drop files whose Hive partition values cannot satisfy the predicate.

    Returns (kept_files, pruned_count). Pure path-based; reads zero bytes.
    """
    if partition_filter is None:
        return files, 0
    kept = [f for f in files if _partition_matches(f.partitions, partition_filter)]
    return kept, len(files) - len(kept)


def _partition_matches(parts: dict[str, object], pred: Predicate) -> bool:
    from .predicates import And, Not, Or

    if isinstance(pred, And):
        return all(_partition_matches(parts, c) for c in pred.children)
    if isinstance(pred, Or):
        return any(_partition_matches(parts, c) for c in pred.children)
    if isinstance(pred, Not):
        return not _partition_matches(parts, pred.child)
    if isinstance(pred, Comparison):
        actual = parts[pred.column]
        return _cmp(actual, pred.op, pred.value)
    if isinstance(pred, In):
        return parts[pred.column] in pred.values
    return True


def _cmp(a, op: str, b) -> bool:
    # Partition values are strings on disk; coerce b to match a's type.
    if isinstance(a, str) and not isinstance(b, str):
        b = str(b)
    return {
        "=": a == b,
        "!=": a != b,
        "<": a < b,
        "<=": a <= b,
        ">": a > b,
        ">=": a >= b,
    }[op]


def read_row_group_metadata(
    fs: pafs.FileSystem, file: ParquetFileInfo
) -> list[RowGroupInfo]:
    """Open footer only and extract per-row-group, per-column statistics.

    This is cheap (footer read) and is what powers row-group pruning. Results
    are cached upstream in Redis keyed by (path, size)."""
    with fs.open_input_file(file.path) as f:
        pf = pq.ParquetFile(f)
        meta = pf.metadata
        schema = pf.schema_arrow
        col_names = [schema.field(i).name for i in range(len(schema))]
        groups: list[RowGroupInfo] = []
        for rg_idx in range(meta.num_row_groups):
            rg = meta.row_group(rg_idx)
            stats: dict[str, ColumnStats] = {}
            col_bytes: dict[str, int] = {}
            for c in range(rg.num_columns):
                col = rg.column(c)
                name = col.path_in_schema.split(".")[0]
                col_bytes[name] = col.total_compressed_size
                st = col.statistics
                if st is not None and st.has_min_max:
                    stats[name] = ColumnStats(
                        min=st.min,
                        max=st.max,
                        null_count=st.null_count,
                        num_values=st.num_values,
                    )
            groups.append(
                RowGroupInfo(
                    file=file,
                    index=rg_idx,
                    num_rows=rg.num_rows,
                    total_byte_size=rg.total_byte_size,
                    column_byte_size=col_bytes,
                    stats=stats,
                )
            )
        # ensure every column appears even if no stats
        for g in groups:
            for name in col_names:
                g.stats.setdefault(name, g.stats.get(name, ColumnStats(None, None)))
        return groups


def read_row_groups(
    fs: pafs.FileSystem,
    path: str,
    row_group_indices: list[int],
    columns: list[str] | None,
    arrow_filter: ds.Expression | None,
) -> pa.Table:
    """Read specific row-groups, selecting only `columns`, then apply the
    filter kernel. This is where column-pruning and predicate-pushdown turn
    into actual reduced I/O and compute."""
    with fs.open_input_file(path) as f:
        pf = pq.ParquetFile(f)
        table = pf.read_row_groups(row_group_indices, columns=columns)
    if arrow_filter is not None:
        table = table.filter(arrow_filter)
    return table
