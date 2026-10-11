"""Page and htmx views for the control plane.

Kept as a module (not a package) so `from apps.frontend import views as
frontend_views` in config/urls.py keeps working. Query-generation logic lives
in demos.py; derived aggregates in metrics.py; list filtering in history.py.
"""

from __future__ import annotations

from django.contrib import messages
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.template.defaultfilters import filesizeformat
from django.urls import reverse

from apps.catalog.models import Dataset
from apps.catalog.services import column_profile, engine_schema, preview_stats
from apps.engine.api import explain_sql
from apps.queries.models import QueryRun
from apps.queries.tasks import run_query

from . import history as history_filters
from . import metrics
from .demos import build_demo_queries

TERMINAL = (QueryRun.Status.SUCCESS, QueryRun.Status.ERROR)


def _is_htmx(request) -> bool:
    return request.headers.get("HX-Request") == "true"


# --- Pages ----------------------------------------------------------------


def dashboard(request):
    """The Overview page. Route name stays `dashboard` so existing
    {% url 'dashboard' %} references and bookmarks keep resolving."""
    stats = metrics.overview_stats()
    duration = metrics.duration_series()
    scan = metrics.scan_series()
    return render(
        request,
        "pages/overview.html",
        {
            "datasets": Dataset.objects.all().order_by("name"),
            "runs": QueryRun.objects.select_related("dataset").all()[:12],
            "stats": stats,
            # json_script keeps chart data out of JS literals entirely.
            "duration_json": duration,
            "scan_json": scan,
            # Metric subtitles are composed here because Django templates
            # cannot interpolate; each one names the window it covers.
            "runs_sub": f"{stats['success_runs']} succeeded, {stats['error_runs']} failed",
            "latency_sub": f"median of {len(duration)} timed runs",
            "scan_sub": (
                f"{_fmt_bytes(stats['bytes_scanned'])} of "
                f"{_fmt_bytes(stats['bytes_available'])} compressed"
                if stats["bytes_available"]
                else "no scans recorded"
            ),
            "success_sub": f"across the last {stats['window']} runs",
            "nav": "overview",
        },
    )


def _fmt_bytes(n: int | None) -> str:
    """Compact byte formatting for composed subtitle strings.

    Django's `filesizeformat` is not reachable from Python here, and it renders
    None as "0 bytes" -- a zero that reads as a measurement.
    """
    if not n:
        return "n/a"
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def datasets(request):
    return render(
        request,
        "pages/datasets.html",
        {"usage": metrics.dataset_usage(), "nav": "datasets"},
    )


def dataset_detail(request, dataset_id: int):
    dataset = get_object_or_404(Dataset, pk=dataset_id)

    # Both of these read a Parquet footer live, so either can fail when the
    # files have moved or bucket credentials are absent. The page degrades to
    # catalog metadata rather than 500ing.
    try:
        stats = preview_stats(dataset)
    except Exception:  # noqa: BLE001
        stats = {}

    profile: dict = {}
    profile_error = ""
    try:
        profile = column_profile(dataset)
    except Exception as exc:  # noqa: BLE001
        profile_error = str(exc)

    demos = build_demo_queries(dataset, stats)
    columns = list(dataset.columns.all())
    partition_total = dataset.partitions.count()
    run_count = dataset.runs.count()

    return render(
        request,
        "pages/dataset_detail.html",
        {
            "dataset": dataset,
            "columns": columns,
            "partitions": dataset.partitions.all()[:200],
            "partition_total": partition_total,
            "stats": stats,
            "profile": profile,
            "profile_error": profile_error,
            "demos": demos,
            "runs": dataset.runs.all()[:25],
            "column_count": len(columns),
            "run_count": run_count,
            # Composed in Python: templates cannot interpolate.
            "size_value": filesizeformat(dataset.total_bytes),
            "files_sub": f"{partition_total} registered in the catalog",
            "columns_sub": (
                f"{len(dataset.partition_columns)} partition, "
                f"{len(columns) - len(dataset.partition_columns)} data"
            ),
            "runs_sub": f"{dataset.runs.filter(status=QueryRun.Status.SUCCESS).count()} succeeded",
            "nav": "datasets",
        },
    )


def workspace(request, dataset_id: int | None = None):
    """The SQL playground. This is the page that has to explain the product:
    write SQL against Parquet in object storage and watch what it avoided
    reading."""
    all_datasets = list(Dataset.objects.all().order_by("name"))
    dataset = None
    if dataset_id is not None:
        dataset = get_object_or_404(Dataset, pk=dataset_id)
    elif all_datasets:
        dataset = all_datasets[0]

    demos: list = []
    columns: list = []
    sql = ""
    if dataset is not None:
        try:
            stats = preview_stats(dataset)
        except Exception:  # noqa: BLE001
            stats = {}
        demos = build_demo_queries(dataset, stats)
        columns = list(dataset.columns.all())
        sql = demos[0].sql if demos else f"SELECT *\nFROM {dataset.name}\nLIMIT 20"

    return render(
        request,
        "pages/workspace.html",
        {
            "datasets": all_datasets,
            "dataset": dataset,
            "columns": columns,
            "demos": demos,
            "sql": sql,
            "nav": "workspace",
        },
    )


def history(request):
    """Searchable, filterable, sortable run history.

    A GET form, so every result set is a shareable URL. Filtering and sorting
    both happen in SQL -- see history.py.
    """
    query = history_filters.parse(request.GET)
    runs = list(history_filters.build(query))
    return render(
        request,
        "pages/history.html",
        {
            "runs": runs,
            "query": query,
            "datasets": Dataset.objects.all().order_by("name"),
            "sort_options": history_filters.sort_options(),
            "status_options": history_filters.status_options(),
            "ratio_sorted": query.sort in history_filters.RATIO_SORTS,
            "result_summary": (
                f"Showing {len(runs)} run{'' if len(runs) == 1 else 's'}, "
                f"capped at {history_filters.PAGE_SIZE}, {query.sort_label.lower()}."
            ),
            "nav": "history",
        },
    )


def plans(request):
    runs = (
        QueryRun.objects.select_related("dataset")
        .exclude(plan_mermaid="")
        .order_by("-created_at")[:25]
    )
    return render(request, "pages/plans.html", {"runs": runs, "nav": "plans"})


def performance(request):
    stats = metrics.overview_stats()
    benchmark = metrics.benchmark_table()
    duration = metrics.duration_series()
    return render(
        request,
        "pages/performance.html",
        {
            "stats": stats,
            "runs": duration,  # presence check only: is there anything to plot
            "duration_json": duration,
            "scan_json": metrics.scan_series(),
            "slowest": metrics.slowest_runs(8),
            "greediest": metrics.most_expensive_scans(8),
            "leanest": metrics.leanest_scans(10),
            "benchmark": benchmark,
            "summary": metrics.benchmark_summary(benchmark),
            # Only timed cases reach the chart; an untimed one would otherwise
            # draw as a zero-length bar beside real ones.
            "benchmark_json": [b for b in (benchmark or []) if b["engine_ms"]],
            "latency_sub": f"over {len(duration)} timed runs",
            "saved_sub": (
                f"{_fmt_bytes(stats['bytes_saved'])} never read"
                if stats["bytes_saved"]
                else "no scans recorded"
            ),
            "nav": "performance",
        },
    )


def capabilities(request):
    return render(request, "pages/capabilities.html", {"nav": "capabilities"})


def query_run(request, run_id: int):
    run = get_object_or_404(QueryRun.objects.select_related("dataset"), pk=run_id)
    return render(
        request,
        "pages/query_detail.html",
        {
            "run": run,
            "done": run.status in TERMINAL,
            "columns": run.result_columns,
            # Django templates cannot interpolate, so the composed heading
            # strings are built here rather than hard-coded per branch.
            "run_title": f"Run #{run.id}",
            "run_subtitle": f"{run.dataset.name} · submitted {run.created_at:%d %b %Y, %H:%M}",
            "export_payload": _export_payload(run),
            "export_dom_id": f"export-data-{run.id}",
            "export_name": f"run-{run.id}",
            "nav": "history",
        },
    )


# --- Query submission -----------------------------------------------------


def submit_query(request):
    """Create a run and dispatch it.

    Two response shapes, one code path. A plain form POST keeps the original
    redirect contract (the curl recipe in my debugging notes depends on it);
    an htmx POST gets the run-status partial rendered straight into the
    workspace, which then self-polls `query_run_status` exactly as before.
    """
    if request.method != "POST":
        return redirect("dashboard")

    dataset_id = request.POST.get("dataset")
    sql = (request.POST.get("sql") or "").strip()
    dataset = get_object_or_404(Dataset, pk=dataset_id)

    if not sql:
        if _is_htmx(request):
            return render(request, "partials/_explain.html", {"error": "SQL cannot be empty."})
        messages.error(request, "SQL cannot be empty")
        return redirect("dataset_detail", dataset_id=dataset.id)

    run = QueryRun.objects.create(dataset=dataset, sql=sql)
    run_query.delay(run.id)

    if _is_htmx(request):
        # With no REDIS_URL, Celery is eager, so the run is already terminal
        # here and the partial renders its final state on the first paint.
        run.refresh_from_db()
        return _render_status(request, run)

    return HttpResponseRedirect(reverse("query_run", args=[run.id]))


def rerun_query(request, run_id: int):
    """Re-execute an existing run's SQL as a new run.

    The four stored `error` runs are all a stale NameError from the
    double-runserver bug in my debugging notes; the code path works now. This
    gives them a way to be re-proven instead of deleting the rows.
    """
    if request.method != "POST":
        return redirect("query_run", run_id=run_id)

    old = get_object_or_404(QueryRun.objects.select_related("dataset"), pk=run_id)
    run = QueryRun.objects.create(dataset=old.dataset, sql=old.sql)
    run_query.delay(run.id)

    if _is_htmx(request):
        run.refresh_from_db()
        return _render_status(request, run)

    return HttpResponseRedirect(reverse("query_run", args=[run.id]))


def query_run_status(request, run_id: int):
    """htmx polling endpoint: returns the live status/results partial."""
    run = get_object_or_404(QueryRun.objects.select_related("dataset"), pk=run_id)
    return _render_status(request, run)


def _render_status(request, run: QueryRun):
    return render(
        request,
        "partials/_run_status.html",
        {
            "run": run,
            "done": run.status in TERMINAL,
            "columns": run.result_columns,
            "export_payload": _export_payload(run),
            # Ids are suffixed with the run id because the workspace can hold
            # a previous run's grid while a new one swaps in.
            "export_dom_id": f"export-data-{run.id}",
            "export_name": f"run-{run.id}",
            # The out-of-band insights swap is only meaningful in an htmx
            # response; in a full page render it would duplicate the rail.
            "oob": True,
        },
    )


def _export_payload(run: QueryRun) -> dict:
    """Raw result values for the CSV/JSON export buttons.

    Deliberately the unformatted values, not the rendered table: the display
    filter prints NULL for a real NULL and rounds floats for legibility, and
    an export should carry neither of those.
    """
    return {"columns": run.result_columns, "rows": run.result_preview}


def explain_query(request):
    """Plan some SQL without executing it.

    Uses the same engine pair as the DRF `QueryRunViewSet.explain` action:
    `explain_sql` over `engine_schema(dataset)`, so no storage I/O beyond
    what the catalog already knows.
    """
    if request.method != "POST":
        return redirect("workspace")

    dataset = get_object_or_404(Dataset, pk=request.POST.get("dataset"))
    sql = (request.POST.get("sql") or "").strip()
    if not sql:
        return render(request, "partials/_explain.html", {"error": "SQL cannot be empty."})

    try:
        out = explain_sql(sql, dataset.uri, schema=engine_schema(dataset))
    except Exception as exc:  # noqa: BLE001
        # Returned with HTTP 200 on purpose: htmx does not swap 4xx/5xx, so a
        # 400 here would look like the Explain button did nothing. The message
        # is the engine's own, and renders through normal autoescaping.
        return render(request, "partials/_explain.html", {"error": str(exc)})

    return render(request, "partials/_explain.html", out)
