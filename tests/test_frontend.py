"""Tests for the control-plane UI: pages, filters, and derived metrics.

QueryRun rows are created directly rather than by executing SQL. These tests
are about the presentation layer, so going through the engine would make them
slow, make them depend on Parquet fixtures, and blame the wrong layer when they
fail -- engine correctness is covered by the DuckDB-oracle tests.

The two things most worth pinning here are the ones that look fine until they
are not: an empty database must still render every page (that is what proves
the empty states exist), and a bytes_total of 0 must not divide by zero.
"""

from __future__ import annotations

import pytest
from django.urls import reverse

from apps.catalog.models import Column, Dataset
from apps.frontend import history, metrics
from apps.queries.models import QueryRun

# Every page the sidebar links to. A missing URL name here would 500 every
# page, since base.html resolves all of them on every render.
PAGES = [
    "dashboard",
    "workspace",
    "datasets",
    "history",
    "plans",
    "performance",
    "capabilities",
]


@pytest.fixture
def dataset(db):
    ds = Dataset.objects.create(
        name="trips",
        uri="./data/trips",
        description="sample",
        file_count=15,
        total_bytes=6_190_367,
    )
    Column.objects.create(dataset=ds, name="trip_id", arrow_type="int64", position=0)
    Column.objects.create(dataset=ds, name="price", arrow_type="double", position=1)
    Column.objects.create(
        dataset=ds, name="region", arrow_type="string", is_partition=True, position=2
    )
    return ds


def make_run(dataset, **kwargs):
    """A successful run with coherent byte accounting unless overridden."""
    defaults = {
        "sql": "SELECT count(*) FROM trips",
        "status": QueryRun.Status.SUCCESS,
        "duration_ms": 50.0,
        "bytes_scanned": 1_000,
        "bytes_total": 10_000,
        "rows_scanned": 300_000,
        "rows_returned": 1,
        "files_pruned": 10,
        "row_groups_pruned": 100,
        "plan_text": "Aggregate\n  Scan trips cols=[passengers]",
        "plan_mermaid": 'graph TD\n    n0["Aggregate"]',
        "result_columns": ["n"],
        "result_preview": [{"n": 300000}],
        "metrics": {
            "total_files": 15,
            "files_scanned": 5,
            "files_pruned": 10,
            "total_row_groups": 120,
            "row_groups_scanned": 20,
            "row_groups_pruned": 100,
            "bytes_scanned": 1_000,
            "bytes_total": 10_000,
            "rows_scanned": 300_000,
            "columns_projected": 1,
            "columns_total": 7,
            "bytes_pruned_files": 5_000,
            "bytes_pruned_row_groups": 3_000,
            "bytes_unprojected": 1_000,
            "scan_map": [],
        },
    }
    defaults.update(kwargs)
    return QueryRun.objects.create(dataset=dataset, **defaults)


# --- Pages render ---------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("name", PAGES)
def test_pages_render_with_empty_database(client, name):
    """Proves the empty states exist. Every one of these pages derives its
    content from runs that do not exist yet."""
    assert client.get(reverse(name)).status_code == 200


@pytest.mark.django_db
@pytest.mark.parametrize("name", PAGES)
def test_pages_render_with_data(client, dataset, name):
    make_run(dataset)
    make_run(dataset, status=QueryRun.Status.ERROR, error="boom", duration_ms=None)
    assert client.get(reverse(name)).status_code == 200


@pytest.mark.django_db
def test_detail_pages_render(client, dataset):
    run = make_run(dataset)
    assert client.get(reverse("query_run", args=[run.id])).status_code == 200
    assert client.get(reverse("query_run_status", args=[run.id])).status_code == 200
    assert client.get(reverse("workspace_dataset", args=[dataset.id])).status_code == 200


@pytest.mark.django_db
def test_dataset_detail_survives_unreadable_storage(client, dataset):
    """The URI points at nothing on disk. The storage panel reads a footer
    live, so it must degrade to catalog metadata rather than 500."""
    dataset.uri = "./does-not-exist"
    dataset.save()
    response = client.get(reverse("dataset_detail", args=[dataset.id]))
    assert response.status_code == 200
    assert b"trip_id" in response.content  # catalog schema still rendered


@pytest.mark.django_db
def test_error_run_shows_its_message_and_rerun(client, dataset):
    run = make_run(
        dataset,
        status=QueryRun.Status.ERROR,
        error="NameError: name '_narrowest_column' is not defined",
        duration_ms=None,
    )
    body = client.get(reverse("query_run", args=[run.id])).content.decode()
    assert "_narrowest_column" in body
    assert reverse("rerun_query", args=[run.id]) in body


@pytest.mark.django_db
def test_null_result_cell_renders_as_null_not_none(client, dataset):
    """The NULL-semantics work in the executor is invisible unless the grid
    renders a real NULL. Python's None must never reach the page."""
    run = make_run(dataset, result_columns=["avg_tip"], result_preview=[{"avg_tip": None}])
    body = client.get(reverse("query_run", args=[run.id])).content.decode()
    assert "NULL" in body
    assert "None" not in body


# --- History filtering and sorting ---------------------------------------


@pytest.mark.django_db
def test_history_search_filters_by_sql(client, dataset):
    make_run(dataset, sql="SELECT count(*) FROM trips")
    make_run(dataset, sql="SELECT region, avg(price) FROM trips GROUP BY region")

    assert len(history.build(history.parse({"q": "GROUP BY"}))) == 1
    assert len(history.build(history.parse({"q": "FROM trips"}))) == 2
    assert len(history.build(history.parse({"q": "nonexistent"}))) == 0


@pytest.mark.django_db
def test_history_filters_by_status_and_dataset(client, dataset):
    other = Dataset.objects.create(name="events", uri="./data/events")
    make_run(dataset)
    make_run(dataset, status=QueryRun.Status.ERROR, error="boom")
    make_run(other)

    assert len(history.build(history.parse({"status": "error"}))) == 1
    assert len(history.build(history.parse({"status": "success"}))) == 2
    assert len(history.build(history.parse({"dataset": str(other.id)}))) == 1


@pytest.mark.django_db
def test_history_sorts_by_duration_and_scan_ratio(dataset):
    make_run(dataset, duration_ms=10.0, bytes_scanned=9_000, bytes_total=10_000)
    make_run(dataset, duration_ms=900.0, bytes_scanned=100, bytes_total=10_000)

    assert history.build(history.parse({"sort": "slowest"}))[0].duration_ms == 900.0
    assert history.build(history.parse({"sort": "fastest"}))[0].duration_ms == 10.0
    # scan_ratio is a Python property, so these only work via the SQL
    # annotation in metrics.with_scan_ratio.
    assert history.build(history.parse({"sort": "greediest"}))[0].ratio == pytest.approx(90.0)
    assert history.build(history.parse({"sort": "leanest"}))[0].ratio == pytest.approx(1.0)


@pytest.mark.django_db
def test_history_rejects_unknown_sort_and_status(dataset):
    """An unrecognised sort key must never reach order_by(), where it would
    raise FieldError and expose column names."""
    make_run(dataset)
    assert history.parse({"sort": "sql injection; drop table"}).sort == "recent"
    assert history.parse({"status": "'; DROP TABLE--"}).status == ""
    assert history.parse({"dataset": "not-a-number"}).dataset_id is None
    assert len(history.build(history.parse({"sort": "bogus"}))) == 1


@pytest.mark.django_db
def test_history_ratio_sort_excludes_unmeasured_runs(dataset):
    """A run with no byte accounting has a NULL ratio. It must be excluded,
    not sorted to an end as though it were a measured extreme."""
    make_run(dataset, bytes_scanned=100, bytes_total=10_000)
    make_run(dataset, bytes_scanned=None, bytes_total=None)

    assert len(history.build(history.parse({"sort": "leanest"}))) == 1
    assert len(history.build(history.parse({"sort": "recent"}))) == 2


@pytest.mark.django_db
def test_history_page_accepts_hostile_query_string(client, dataset):
    make_run(dataset)
    response = client.get(
        reverse("history"),
        {
            "sort": "../../etc/passwd",
            "status": "' OR 1=1",
            "dataset": "99999999",
            "q": "<script>alert(1)</script>",
        },
    )
    assert response.status_code == 200
    body = response.content.decode()
    # The search box reflects the term, so it must come back escaped. Asserting
    # on the payload rather than on "<script" alone: the page legitimately ends
    # with its own <script src="..."> tags.
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
    assert "<script>alert(1)" not in body


# --- Derived metrics ------------------------------------------------------


@pytest.mark.django_db
def test_overview_stats_against_hand_computed_values(dataset):
    make_run(dataset, duration_ms=10.0, bytes_scanned=1_000, bytes_total=10_000)
    make_run(dataset, duration_ms=20.0, bytes_scanned=2_000, bytes_total=10_000)
    make_run(dataset, duration_ms=60.0, bytes_scanned=3_000, bytes_total=10_000)
    make_run(dataset, status=QueryRun.Status.ERROR, error="boom", duration_ms=None)

    stats = metrics.overview_stats()
    assert stats["total_runs"] == 4
    assert stats["success_runs"] == 3
    assert stats["error_runs"] == 1
    assert stats["median_ms"] == 20.0
    assert stats["fastest_ms"] == 10.0
    assert stats["slowest_ms"] == 60.0
    assert stats["success_pct"] == 75.0
    # Byte-weighted, not a mean of percentages: 6000 read of 30000 available.
    assert stats["scan_ratio"] == 20.0
    assert stats["saved_pct"] == 80.0
    assert stats["bytes_saved"] == 24_000


@pytest.mark.django_db
def test_overview_stats_reports_none_not_zero_when_empty(db):
    stats = metrics.overview_stats()
    assert stats["total_runs"] == 0
    # None renders as "Not reported"; 0 would read as a real measurement.
    assert stats["median_ms"] is None
    assert stats["scan_ratio"] is None
    assert stats["success_pct"] is None
    assert stats["bytes_scanned"] is None


@pytest.mark.django_db
def test_with_scan_ratio_returns_none_instead_of_dividing_by_zero(dataset):
    make_run(dataset, bytes_scanned=0, bytes_total=0)
    make_run(dataset, bytes_scanned=None, bytes_total=None)
    make_run(dataset, bytes_scanned=2_500, bytes_total=10_000)

    ratios = sorted(
        (r.ratio for r in metrics.with_scan_ratio(QueryRun.objects.all())),
        key=lambda v: (v is not None, v),
    )
    assert ratios[0] is None
    assert ratios[1] is None
    assert ratios[2] == pytest.approx(25.0)


@pytest.mark.django_db
def test_series_are_oldest_first(dataset):
    first = make_run(dataset, duration_ms=10.0)
    second = make_run(dataset, duration_ms=20.0)

    # Charts read left to right, so the oldest point has to come first.
    assert [p["id"] for p in metrics.duration_series()] == [first.id, second.id]
    assert [p["id"] for p in metrics.scan_series()] == [first.id, second.id]


@pytest.mark.django_db
def test_scan_series_decomposes_into_read_plus_saved(dataset):
    make_run(dataset, bytes_scanned=2_500, bytes_total=10_000)
    point = metrics.scan_series()[0]
    assert point["read"] == 2_500
    assert point["saved"] == 7_500
    assert point["read"] + point["saved"] == 10_000
    assert point["ratio"] == 25.0


@pytest.mark.django_db
def test_dataset_usage_handles_a_dataset_with_no_runs(dataset):
    usage = metrics.dataset_usage()
    assert len(usage) == 1
    assert usage[0]["run_count"] == 0
    assert usage[0]["median_ms"] is None
    assert usage[0]["scan_ratio"] is None


def test_benchmark_table_returns_none_when_file_missing(settings, tmp_path):
    """None, so the panel shows an empty state. Fabricating a comparison from
    live timings would be measuring different data under a different load."""
    settings.BASE_DIR = tmp_path
    assert metrics.benchmark_table() is None
    assert metrics.benchmark_summary(None) is None


def test_benchmark_table_reports_timing_and_byte_accounting(settings, tmp_path):
    """The scan ratio is what the page is built around, so it is derived here
    rather than trusted from the file: bytes_scanned over bytes_total, both
    compressed. Unrelated top-level keys must be ignored, not crash the loader."""
    (tmp_path / "benchmark_results.json").write_text(
        '{"engine": {"agg_by_region": {"ms": 68.5, "metrics": '
        '{"bytes_scanned": 3000, "bytes_total": 6000, "columns_projected": 2, '
        '"columns_total": 7}}}, "duckdb": {"agg_by_region": {"ms": 19.3}}}',
        encoding="utf-8",
    )
    settings.BASE_DIR = tmp_path

    rows = metrics.benchmark_table()
    assert len(rows) == 1
    row = rows[0]
    assert row["engine_ms"] == 68.5
    assert row["scan_ratio"] == 50.0
    assert row["columns_projected"] == 2

    summary = metrics.benchmark_summary(rows)
    assert summary["cases"] == 1
    assert summary["fastest_ms"] == 68.5
    assert summary["slowest_ms"] == 68.5


# --- Query submission -----------------------------------------------------


@pytest.mark.django_db
def test_submit_query_redirects_for_a_plain_form_post(client, dataset, monkeypatch):
    """The original contract. The curl recipe in my debugging notes and any
    non-htmx client depend on this 302."""
    monkeypatch.setattr("apps.frontend.views.run_query.delay", lambda run_id: None)
    response = client.post(
        reverse("submit_query"), {"dataset": dataset.id, "sql": "SELECT 1 FROM trips"}
    )
    assert response.status_code == 302
    assert response["Location"] == reverse("query_run", args=[QueryRun.objects.first().id])


@pytest.mark.django_db
def test_submit_query_returns_the_partial_for_htmx(client, dataset, monkeypatch):
    monkeypatch.setattr("apps.frontend.views.run_query.delay", lambda run_id: None)
    response = client.post(
        reverse("submit_query"),
        {"dataset": dataset.id, "sql": "SELECT 1 FROM trips"},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 200
    body = response.content.decode()
    assert 'id="run-status"' in body
    # One response updates both the results pane and the insights rail.
    assert "hx-swap-oob" in body


@pytest.mark.django_db
def test_empty_sql_is_rejected_without_creating_a_run(client, dataset):
    client.post(reverse("submit_query"), {"dataset": dataset.id, "sql": "   "})
    assert QueryRun.objects.count() == 0


@pytest.mark.django_db
def test_rerun_creates_a_new_run_and_keeps_the_original(client, dataset, monkeypatch):
    """The stored error runs are kept as a record. Rerunning must add a row,
    never mutate or delete the one being re-proven."""
    monkeypatch.setattr("apps.frontend.views.run_query.delay", lambda run_id: None)
    original = make_run(dataset, status=QueryRun.Status.ERROR, error="stale NameError")

    response = client.post(reverse("rerun_query", args=[original.id]))
    assert response.status_code == 302
    assert QueryRun.objects.count() == 2

    original.refresh_from_db()
    assert original.status == QueryRun.Status.ERROR
    assert QueryRun.objects.exclude(pk=original.pk).get().sql == original.sql


@pytest.mark.django_db
def test_explain_returns_a_plan_without_executing(client, dataset):
    response = client.post(
        reverse("explain_query"),
        {"dataset": dataset.id, "sql": "SELECT count(*) FROM trips"},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 200
    assert b"mermaid" in response.content
    assert QueryRun.objects.count() == 0  # explain never records a run


@pytest.mark.django_db
def test_explain_reports_the_engine_message_at_http_200(client, dataset):
    """200, not 400: htmx does not swap 4xx responses, so an error status
    would make the Explain button look like it did nothing."""
    response = client.post(
        reverse("explain_query"),
        {"dataset": dataset.id, "sql": "SELECT FROM WHERE"},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 200
    assert b"errbox" in response.content
