from django.contrib import messages
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from apps.catalog.models import Dataset
from apps.catalog.services import preview_stats
from apps.queries.models import QueryRun
from apps.queries.tasks import run_query


def dashboard(request):
    datasets = Dataset.objects.all().order_by("name")
    runs = QueryRun.objects.select_related("dataset").all()[:20]
    return render(
        request,
        "dashboard.html",
        {"datasets": datasets, "runs": runs},
    )


def dataset_detail(request, dataset_id: int):
    dataset = get_object_or_404(Dataset, pk=dataset_id)
    try:
        stats = preview_stats(dataset)
    except Exception:  # noqa: BLE001
        stats = {}
    sample_sql = _sample_query(dataset)
    return render(
        request,
        "dataset_detail.html",
        {
            "dataset": dataset,
            "columns": dataset.columns.all(),
            "partitions": dataset.partitions.all()[:50],
            "stats": stats,
            "sample_sql": sample_sql,
        },
    )


def submit_query(request):
    if request.method != "POST":
        return redirect("dashboard")
    dataset_id = request.POST.get("dataset")
    sql = (request.POST.get("sql") or "").strip()
    dataset = get_object_or_404(Dataset, pk=dataset_id)
    if not sql:
        messages.error(request, "SQL cannot be empty")
        return redirect("dataset_detail", dataset_id=dataset.id)
    run = QueryRun.objects.create(dataset=dataset, sql=sql)
    run_query.delay(run.id)
    return HttpResponseRedirect(reverse("query_run", args=[run.id]))


def query_run(request, run_id: int):
    run = get_object_or_404(QueryRun.objects.select_related("dataset"), pk=run_id)
    return render(request, "query_run.html", {"run": run})


def query_run_status(request, run_id: int):
    """htmx polling endpoint: returns the live status/results partial."""
    run = get_object_or_404(QueryRun.objects.select_related("dataset"), pk=run_id)
    done = run.status in (QueryRun.Status.SUCCESS, QueryRun.Status.ERROR)
    return render(
        request,
        "_run_status.html",
        {"run": run, "done": done, "columns": run.result_columns},
    )


def _sample_query(dataset: Dataset) -> str:
    cols = dataset.column_names
    parts = dataset.partition_columns
    name = dataset.name
    if "price" in cols and parts:
        p = parts[0]
        return (
            f"SELECT {p}, COUNT(*) AS n, SUM(price) AS revenue, AVG(price) AS avg_price\n"
            f"FROM {name}\n"
            f"WHERE price > 50\n"
            f"GROUP BY {p}\n"
            f"ORDER BY revenue DESC"
        )
    select_cols = ", ".join(cols[:3]) if cols else "*"
    return f"SELECT {select_cols}\nFROM {name}\nLIMIT 50"
