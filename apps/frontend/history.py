"""Server-side search / filter / sort for the Query History page.

All of it happens in SQL. The alternative -- shipping every run to the client
and filtering in JS -- would be a lie about how the page scales, and the sort
on scan ratio is not expressible client-side anyway without re-deriving it.

`scan_ratio` is a Python @property on QueryRun, so sorting by it requires the
SQL annotation in metrics.with_scan_ratio rather than `order_by("scan_ratio")`,
which Django rejects.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db.models import Q

from apps.queries.models import QueryRun

from .metrics import with_scan_ratio

PAGE_SIZE = 100

# Only these sort keys are honoured. An unrecognised value falls back to
# "recent" rather than reaching order_by(), so a crafted ?sort= cannot
# enumerate columns or trigger a FieldError.
SORTS = {
    "recent": ("-created_at", "Newest first"),
    "oldest": ("created_at", "Oldest first"),
    "slowest": ("-duration_ms", "Slowest first"),
    "fastest": ("duration_ms", "Fastest first"),
    "biggest": ("-bytes_scanned", "Most bytes read"),
    "smallest": ("bytes_scanned", "Fewest bytes read"),
    "greediest": ("-ratio", "Highest share of dataset read"),
    "leanest": ("ratio", "Lowest share of dataset read"),
}

RATIO_SORTS = {"greediest", "leanest"}


@dataclass
class HistoryQuery:
    """The parsed, validated state of the history filter form."""

    search: str = ""
    dataset_id: int | None = None
    status: str = ""
    sort: str = "recent"

    @property
    def is_filtered(self) -> bool:
        return bool(self.search or self.dataset_id or self.status) or self.sort != "recent"

    @property
    def sort_label(self) -> str:
        return SORTS[self.sort][1]


def parse(params) -> HistoryQuery:
    """Read a QueryDict into a HistoryQuery, discarding anything invalid."""
    raw_status = (params.get("status") or "").strip()
    status = raw_status if raw_status in QueryRun.Status.values else ""

    raw_sort = (params.get("sort") or "").strip()
    sort = raw_sort if raw_sort in SORTS else "recent"

    dataset_id = None
    raw_dataset = (params.get("dataset") or "").strip()
    if raw_dataset.isdigit():
        dataset_id = int(raw_dataset)

    return HistoryQuery(
        search=(params.get("q") or "").strip()[:200],
        dataset_id=dataset_id,
        status=status,
        sort=sort,
    )


def build(query: HistoryQuery, limit: int = PAGE_SIZE):
    """Turn a HistoryQuery into an ordered, sliced queryset."""
    qs = QueryRun.objects.select_related("dataset")

    if query.search:
        qs = qs.filter(Q(sql__icontains=query.search) | Q(error__icontains=query.search))
    if query.dataset_id:
        qs = qs.filter(dataset_id=query.dataset_id)
    if query.status:
        qs = qs.filter(status=query.status)

    field = SORTS[query.sort][0]
    if query.sort in RATIO_SORTS:
        # Rows with no byte accounting have a NULL ratio and would sort to one
        # end as if they were measured extremes. Exclude them instead: the
        # footer says so, and a NULL is not a "lowest share".
        qs = with_scan_ratio(qs).filter(bytes_total__gt=0)
    elif "duration_ms" in field:
        qs = qs.filter(duration_ms__isnull=False)
    elif "bytes_scanned" in field:
        qs = qs.filter(bytes_scanned__isnull=False)

    return qs.order_by(field)[:limit]


def sort_options() -> list[dict]:
    return [{"value": k, "label": label} for k, (_, label) in SORTS.items()]


def status_options() -> list[dict]:
    return [{"value": s, "label": s} for s in QueryRun.Status.values]
