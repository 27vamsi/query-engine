"""End-to-end: explain + run through the public API, like the Celery task does."""

from apps.engine.api import explain_sql, run_sql


def test_explain_returns_plan_and_mermaid(trips_dataset):
    out = explain_sql(
        "SELECT region, SUM(price) AS rev FROM trips WHERE price > 50 GROUP BY region",
        trips_dataset,
    )
    assert "Aggregate" in out["plan_text"]
    assert "Scan" in out["plan_text"]
    assert out["plan_mermaid"].startswith("graph TD")


def test_run_returns_metrics_and_plan(trips_dataset):
    r = run_sql(
        "SELECT region, COUNT(*) AS n FROM trips WHERE region = 'US' GROUP BY region",
        trips_dataset,
        max_workers=1,
    )
    assert r.columns == ["region", "n"]
    assert r.rows and r.rows[0]["region"] == "US"
    assert r.metrics["files_pruned"] == 6
    assert r.duration_ms >= 0
    assert "pushdown" in r.plan_text or "part_filter" in r.plan_text


def test_parallel_matches_serial(trips_dataset):
    sql = "SELECT payment, SUM(price) AS s, COUNT(*) AS n FROM trips GROUP BY payment"
    serial = run_sql(sql, trips_dataset, max_workers=1)
    parallel = run_sql(sql, trips_dataset, max_workers=4)

    def key(rows):
        # payment is nullable, so the rows are not naturally orderable
        return sorted(((r["payment"], round(r["s"], 2), r["n"]) for r in rows), key=repr)

    assert key(serial.rows) == key(parallel.rows)
