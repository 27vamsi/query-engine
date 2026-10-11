"""Derived aggregates over QueryRun, plus the benchmark-file loader.

Pure functions with no template logic, so each one is directly unit-testable.
Two things this module exists to work around:

1. `QueryRun.scan_ratio` is a Python @property, not a column, so it cannot
   appear in `order_by()` or `aggregate()`. `with_scan_ratio` annotates the
   same arithmetic in SQL instead.
2. SQLite has no median aggregate, so the median is computed in Python over a
   bounded `values_list`.

Every page that uses these states its window explicitly -- "across the last N
runs" -- because an unqualified average over all of history is a number nobody
can check.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path

from django.conf import settings
from django.db.models import Case, F, FloatField, When
from django.db.models.functions import Cast

from apps.queries.models import QueryRun

WINDOW = 200

# The five cases in benchmark_results.json, in the order the README presents
# them, with the shape of each query spelled out. Keys absent from the file are
# skipped rather than rendered as a gap.
BENCHMARK_LABELS = {
    "agg_by_region": ("Group by partition column", "GROUP BY region"),
    "filtered_agg": ("Filtered aggregate", "WHERE + GROUP BY"),
    "selective_scan": ("Selective row-group scan", "narrow WHERE on a sorted column"),
    "partition_filter": ("Partition-pruned count", "WHERE on a partition column"),
    "wide_group": ("High-cardinality group", "GROUP BY pickup_hour"),
}


def with_scan_ratio(qs):
    """Annotate `ratio` = 100 * bytes_scanned / bytes_total, in SQL.

    The When guard is the point: it keeps the division out of the query plan
    entirely for rows where bytes_total is 0 or NULL, which would otherwise be
    a divide-by-zero. Those rows get NULL, matching `QueryRun.scan_ratio`.
    """
    return qs.annotate(
        ratio=Case(
            When(
                bytes_total__gt=0,
                then=100.0 * Cast(F("bytes_scanned"), FloatField()) / Cast(F("bytes_total"), FloatField()),
            ),
            default=None,
            output_field=FloatField(),
        )
    )


def _recent(window: int = WINDOW, **filters):
    """The most recent `window` runs matching `filters`.

    Filters are applied before the slice: a sliced queryset cannot be filtered
    again (Django raises), and "the last 200 successful runs" is the useful
    window anyway -- not "whichever of the last 200 runs happened to succeed".
    """
    return QueryRun.objects.filter(**filters).order_by("-created_at")[:window]


def overview_stats(window: int = WINDOW) -> dict:
    """Headline numbers for the Overview hero and metric cards.

    Any value that cannot be computed comes back as None so the template can
    say "Not reported" instead of printing a zero that looks measured.
    """
    runs = list(
        _recent(window).values(
            "status", "duration_ms", "bytes_scanned", "bytes_total", "rows_scanned"
        )
    )
    total = len(runs)
    ok = [r for r in runs if r["status"] == QueryRun.Status.SUCCESS]

    durations = [r["duration_ms"] for r in ok if r["duration_ms"] is not None]
    median_ms = round(statistics.median(durations), 1) if durations else None

    scanned = sum(r["bytes_scanned"] or 0 for r in ok if r["bytes_total"])
    available = sum(r["bytes_total"] or 0 for r in ok if r["bytes_total"])
    # Bytes read as a share of the bytes those same queries could have read.
    # Weighted by size on purpose: a mean of per-run percentages would let a
    # tiny query count as much as a full scan.
    scan_ratio = round(100 * scanned / available, 1) if available else None
    saved_pct = round(100 - scan_ratio, 1) if scan_ratio is not None else None

    return {
        "window": window,
        "total_runs": total,
        "success_runs": len(ok),
        "error_runs": sum(1 for r in runs if r["status"] == QueryRun.Status.ERROR),
        "success_pct": round(100 * len(ok) / total, 1) if total else None,
        "median_ms": median_ms,
        "fastest_ms": round(min(durations), 1) if durations else None,
        "slowest_ms": round(max(durations), 1) if durations else None,
        "scan_ratio": scan_ratio,
        "saved_pct": saved_pct,
        "bytes_scanned": scanned or None,
        "bytes_available": available or None,
        "bytes_saved": (available - scanned) if available else None,
        "rows_scanned": sum(r["rows_scanned"] or 0 for r in ok) or None,
    }


def duration_series(window: int = WINDOW) -> list[dict]:
    """Oldest-first duration points for the latency chart."""
    rows = _recent(
        window, status=QueryRun.Status.SUCCESS, duration_ms__isnull=False
    ).values("id", "duration_ms")
    return [{"id": r["id"], "ms": round(r["duration_ms"], 1)} for r in reversed(list(rows))]


def scan_series(window: int = WINDOW) -> list[dict]:
    """Oldest-first bytes-read vs bytes-available points for the scan chart."""
    rows = _recent(window, status=QueryRun.Status.SUCCESS, bytes_total__gt=0).values(
        "id", "bytes_scanned", "bytes_total"
    )
    out = []
    for r in reversed(list(rows)):
        total = r["bytes_total"]
        read = r["bytes_scanned"] or 0
        out.append(
            {
                "id": r["id"],
                "read": read,
                "saved": max(total - read, 0),
                "ratio": round(100 * read / total, 1),
            }
        )
    return out


def slowest_runs(limit: int = 10):
    return (
        QueryRun.objects.select_related("dataset")
        .filter(status=QueryRun.Status.SUCCESS, duration_ms__isnull=False)
        .order_by("-duration_ms")[:limit]
    )


def fastest_runs(limit: int = 10):
    return (
        QueryRun.objects.select_related("dataset")
        .filter(status=QueryRun.Status.SUCCESS, duration_ms__isnull=False)
        .order_by("duration_ms")[:limit]
    )


def most_expensive_scans(limit: int = 10):
    """Highest share of the dataset read -- the queries that pruned least."""
    qs = QueryRun.objects.select_related("dataset").filter(
        status=QueryRun.Status.SUCCESS, bytes_total__gt=0
    )
    return with_scan_ratio(qs).order_by("-ratio")[:limit]


def leanest_scans(limit: int = 10):
    qs = QueryRun.objects.select_related("dataset").filter(
        status=QueryRun.Status.SUCCESS, bytes_total__gt=0
    )
    return with_scan_ratio(qs).order_by("ratio")[:limit]


def dataset_usage():
    """Per-dataset run counts and scan efficiency, for the Datasets page."""
    from apps.catalog.models import Dataset

    out = []
    for ds in Dataset.objects.all().order_by("name"):
        runs = ds.runs.filter(status=QueryRun.Status.SUCCESS)
        vals = list(runs.values("duration_ms", "bytes_scanned", "bytes_total"))
        available = sum(v["bytes_total"] or 0 for v in vals if v["bytes_total"])
        scanned = sum(v["bytes_scanned"] or 0 for v in vals if v["bytes_total"])
        durations = [v["duration_ms"] for v in vals if v["duration_ms"] is not None]
        out.append(
            {
                "dataset": ds,
                "run_count": ds.runs.count(),
                "median_ms": round(statistics.median(durations), 1) if durations else None,
                "scan_ratio": round(100 * scanned / available, 1) if available else None,
            }
        )
    return out


def benchmark_table() -> list[dict] | None:
    """Load benchmark_results.json into per-query-shape timing + byte rows.

    Returns None when the file is missing or unreadable so the panel can show
    an empty state rather than invent numbers.
    """
    path = Path(settings.BASE_DIR) / "benchmark_results.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

    engine = data.get("engine") or {}
    if not engine:
        return None

    rows = []
    keys = list(BENCHMARK_LABELS) + [k for k in engine if k not in BENCHMARK_LABELS]
    for key in keys:
        if key not in engine:
            continue
        e = engine[key] or {}
        e_ms = e.get("ms")
        label, shape = BENCHMARK_LABELS.get(key, (key.replace("_", " "), ""))
        m = e.get("metrics") or {}
        total = m.get("bytes_total") or 0
        rows.append(
            {
                "key": key,
                "label": label,
                "shape": shape,
                "engine_ms": e_ms,
                "bytes_scanned": m.get("bytes_scanned"),
                "bytes_total": total or None,
                "scan_ratio": (
                    round(100 * (m.get("bytes_scanned") or 0) / total, 1) if total else None
                ),
                "rows_scanned": m.get("rows_scanned"),
                "columns_projected": m.get("columns_projected"),
                "columns_total": m.get("columns_total"),
            }
        )
    return rows or None


def benchmark_summary(rows: list[dict] | None) -> dict | None:
    """One-line read on the benchmark: case count and the latency range."""
    if not rows:
        return None
    timings = [r["engine_ms"] for r in rows if r["engine_ms"]]
    if not timings:
        return None
    return {
        "cases": len(rows),
        "fastest_ms": min(timings),
        "slowest_ms": max(timings),
    }
