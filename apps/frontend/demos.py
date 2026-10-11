"""One-click demo queries that make a single optimization visible at a time.

Moved verbatim out of views.py: this is query-generation logic, not request
handling, and both the Workspace and the Dataset Detail page need it.
"""

from dataclasses import dataclass

from apps.catalog.models import Dataset


@dataclass(frozen=True)
class DemoQuery:
    """A one-click query chosen to make exactly one optimization visible."""

    title: str
    teaches: str
    sql: str


_NUMERIC_HINTS = ("int", "float", "double", "decimal")


def build_demo_queries(dataset: Dataset, stats: dict) -> list[DemoQuery]:
    """Generate a guided tour of the scan path for whatever shape this dataset is.

    The tour is ordered so that a visitor clicking straight down the list sees
    the baseline first and each optimization layered on top of it.
    """
    name = dataset.name
    partition_col = next(iter(dataset.partition_columns), None)
    numeric = [
        c
        for c in dataset.columns.all()
        if not c.is_partition and any(h in c.arrow_type.lower() for h in _NUMERIC_HINTS)
    ]
    numeric_cols = [c.name for c in numeric]
    # Continuous columns make better-looking measures than integers, which tend
    # to be codes, counts or hours rather than something worth averaging.
    continuous = [
        c.name for c in numeric if any(h in c.arrow_type.lower() for h in ("float", "double", "decimal"))
    ]
    # Two different jobs, so two different columns. An id-like column is usually
    # written in ascending order, which makes its per-row-group min/max ranges
    # tight and non-overlapping -- exactly what makes pruning dramatic. A measure
    # column is the one that's actually meaningful to sum or average.
    filter_col = next(
        (c for c in numeric_cols if _is_id_like(c)),
        numeric_cols[0] if numeric_cols else None,
    )
    measure_col = next(
        (c for c in continuous if not _is_id_like(c)),
        next((c for c in numeric_cols if not _is_id_like(c)), filter_col),
    )
    partition_value = None
    if partition_col:
        first = dataset.partitions.first()
        if first:
            partition_value = first.values.get(partition_col)
    threshold = _selective_threshold(stats, filter_col) if filter_col else None

    demos: list[DemoQuery] = []

    demos.append(
        DemoQuery(
            title="Full scan (baseline)",
            teaches=(
                "Every column, no WHERE clause — nothing can be pruned, so this reads 100% of "
                "the dataset. Run it first: it is the number every other query is measured "
                "against."
            ),
            sql=f"SELECT *\nFROM {name}\nLIMIT 20",
        )
    )

    if partition_col and partition_value is not None:
        demos.append(
            DemoQuery(
                title="Partition pruning",
                teaches=(
                    f"{partition_col} is a partition column — it lives in the directory path, not "
                    f"inside the files. The engine drops every file that cannot match without "
                    f"opening a single one."
                ),
                sql=(
                    f"SELECT COUNT(*) AS total\n"
                    f"FROM {name}\n"
                    f"WHERE {partition_col} = '{partition_value}'"
                ),
            )
        )

    if filter_col and threshold is not None:
        demos.append(
            DemoQuery(
                title="Row-group skipping",
                teaches=(
                    f"A highly selective filter on {filter_col}. Every Parquet footer carries "
                    f"min/max statistics per row-group, so most row-groups can be proven "
                    f"irrelevant and skipped before a single byte is decompressed."
                ),
                sql=(
                    f"SELECT COUNT(*) AS total\n"
                    f"FROM {name}\n"
                    f"WHERE {filter_col} > {threshold}"
                ),
            )
        )

    if partition_col and measure_col:
        demos.append(
            DemoQuery(
                title="Column projection",
                teaches=(
                    f"Only {measure_col} is referenced, so only {measure_col} comes off disk — "
                    f"and {partition_col} is free because it comes from the path, not the data. "
                    f"Parquet is columnar, so the unreferenced columns are never touched."
                ),
                sql=(
                    f"SELECT {partition_col}, COUNT(*) AS n, AVG({measure_col}) AS avg_{measure_col}\n"
                    f"FROM {name}\n"
                    f"GROUP BY {partition_col}\n"
                    f"ORDER BY n DESC"
                ),
            )
        )

    if partition_col and partition_value is not None and measure_col and threshold is not None:
        demos.append(
            DemoQuery(
                title="All three at once",
                teaches=(
                    "A query someone would actually write, with all three optimizations firing. "
                    "It reads more than the single-optimization demos above because it needs a "
                    "real measure column rather than just a row count — but still a small "
                    "fraction of the baseline."
                ),
                sql=(
                    f"SELECT {partition_col}, COUNT(*) AS n, SUM({measure_col}) AS total_{measure_col}\n"
                    f"FROM {name}\n"
                    f"WHERE {partition_col} = '{partition_value}' AND {filter_col} > {threshold}\n"
                    f"GROUP BY {partition_col}"
                ),
            )
        )

    return demos


def _is_id_like(column: str) -> bool:
    name = column.lower()
    return name == "id" or name.endswith("_id") or name.startswith("id_")


def _selective_threshold(stats: dict, column: str) -> int | float | None:
    """A cut near the top of a column's observed range.

    Picking 95% of the way up means the predicate matches few row-groups, which
    is what makes min/max pruning show a dramatic number instead of a boring one.
    """
    bounds = stats.get(column) or {}
    low, high = bounds.get("min"), bounds.get("max")
    if isinstance(low, bool) or isinstance(high, bool):
        return None
    if not isinstance(low, (int, float)) or not isinstance(high, (int, float)):
        return None
    if high <= low:
        return None
    cut = low + 0.95 * (high - low)
    return int(cut) if isinstance(low, int) and isinstance(high, int) else round(cut, 2)
