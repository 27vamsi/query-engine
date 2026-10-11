"""Catalog services: register a dataset by inspecting its Parquet files.

This is the bridge between the Django control plane and the storage-facing part
of the engine. We read footers (cheap) to learn the schema, partition columns,
and file inventory, then persist that as Dataset/Column/Partition rows so the
planner has metadata without re-listing storage on every query.
"""

from __future__ import annotations

import pyarrow as pa

from apps.engine.parquet_reader import (
    list_files,
    read_row_group_metadata,
    resolve_fs,
)

from .models import Column, Dataset, Partition


def register_dataset(name: str, uri: str, description: str = "", owner=None) -> Dataset:
    fs, base = resolve_fs(uri)
    files = list_files(fs, base)
    if not files:
        raise ValueError(f"no parquet files found under {uri}")

    sample = files[0]
    with fs.open_input_file(sample.path) as f:
        import pyarrow.parquet as pq

        schema: pa.Schema = pq.ParquetFile(f).schema_arrow

    partition_cols = list(sample.partitions.keys())

    dataset, _ = Dataset.objects.update_or_create(
        name=name,
        defaults={
            "uri": uri,
            "description": description,
            "owner": owner,
            "file_count": len(files),
            "total_bytes": sum(f.size for f in files),
        },
    )

    dataset.columns.all().delete()
    position = 0
    for field in schema:
        Column.objects.create(
            dataset=dataset,
            name=field.name,
            arrow_type=str(field.type),
            is_partition=False,
            position=position,
        )
        position += 1
    for pcol in partition_cols:
        Column.objects.create(
            dataset=dataset,
            name=pcol,
            arrow_type="string",
            is_partition=True,
            position=position,
        )
        position += 1

    dataset.partitions.all().delete()
    partition_rows = [
        Partition(dataset=dataset, path=f.path, values=f.partitions, size_bytes=f.size)
        for f in files
    ]
    Partition.objects.bulk_create(partition_rows, batch_size=500)

    return dataset


def engine_schema(dataset: Dataset):
    """Build the engine's DatasetSchema from stored catalog rows (no I/O)."""
    from apps.engine.api import DatasetSchema

    return DatasetSchema(
        columns=dataset.column_names,
        partition_columns=dataset.partition_columns,
    )


def preview_stats(dataset: Dataset, limit: int = 5) -> dict:
    """Read footer stats of one file for the UI's schema panel."""
    fs, _ = resolve_fs(dataset.uri)
    first = dataset.partitions.first()
    if first is None:
        return {}
    from apps.engine.parquet_reader import ParquetFileInfo

    info = ParquetFileInfo(path=first.path, size=first.size_bytes, partitions=first.values)
    groups = read_row_group_metadata(fs, info)
    out = {}
    for col, st in list(groups[0].stats.items())[:limit] if groups else []:
        out[col] = {"min": _safe(st.min), "max": _safe(st.max)}
    return out


def column_profile(dataset: Dataset) -> dict:
    """Per-column compressed on-disk size from one file's footer.

    This is what makes the column-projection story checkable: it shows which
    column is actually narrowest on disk, which is the column a COUNT(*)
    resolves to. Footer-only, one file, so it is cheap enough for a page load
    -- but it IS live storage I/O, so callers must handle failure.

    Returns {} when the dataset has no registered files.
    """
    fs, _ = resolve_fs(dataset.uri)
    first = dataset.partitions.first()
    if first is None:
        return {}

    from apps.engine.parquet_reader import ParquetFileInfo

    info = ParquetFileInfo(path=first.path, size=first.size_bytes, partitions=first.values)
    groups = read_row_group_metadata(fs, info)
    if not groups:
        return {}

    per_column: dict[str, int] = {}
    for group in groups:
        for col, size in group.column_byte_size.items():
            per_column[col] = per_column.get(col, 0) + size

    file_bytes = sum(per_column.values())
    rows = sum(g.num_rows for g in groups)
    columns = [
        {
            "name": col,
            "bytes": size,
            # Share of this file's column bytes, which is what determines how
            # much a projection actually saves.
            "pct": round(100 * size / file_bytes, 1) if file_bytes else None,
            "bytes_per_row": round(size / rows, 2) if rows else None,
        }
        for col, size in sorted(per_column.items(), key=lambda kv: -kv[1])
    ]
    return {
        "path": first.path,
        "row_groups": len(groups),
        "rows": rows,
        "file_bytes": file_bytes,
        "rows_per_group": round(rows / len(groups)) if groups else None,
        "columns": columns,
        "widest": columns[0] if columns else None,
        "narrowest": columns[-1] if columns else None,
    }


def _safe(v):
    try:
        import json

        json.dumps(v)
        return v
    except (TypeError, ValueError):
        return str(v)
