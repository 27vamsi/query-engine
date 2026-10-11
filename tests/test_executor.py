"""Correctness against DuckDB + assertions that pruning actually happens."""

from apps.engine.api import run_sql
from .conftest import assert_matches

QUERIES = [
    "SELECT region, COUNT(*) AS n, SUM(price) AS rev FROM trips GROUP BY region",
    "SELECT payment, AVG(tip) AS t, MIN(price) AS lo, MAX(price) AS hi FROM trips GROUP BY payment",
    "SELECT region, COUNT(*) AS n FROM trips WHERE price > 80 GROUP BY region",
    "SELECT COUNT(*) AS n FROM trips WHERE region = 'EU'",
    "SELECT region, SUM(distance) AS d FROM trips WHERE payment = 'card' AND distance > 5 GROUP BY region",
]


def test_aggregate_correctness(trips_dataset, oracle):
    for sql in QUERIES:
        result = run_sql(sql, trips_dataset, max_workers=1)
        assert_matches(result, oracle, sql)


def test_projection_query_correctness(trips_dataset, oracle):
    sql = "SELECT trip_id, price FROM trips WHERE trip_id < 10 ORDER BY trip_id"
    result = run_sql(sql, trips_dataset, max_workers=1)
    assert_matches(result, oracle, sql)


def test_partition_pruning_skips_files(trips_dataset):
    result = run_sql(
        "SELECT COUNT(*) AS n FROM trips WHERE region = 'EU'", trips_dataset, max_workers=1
    )
    # 3 regions x 3 dates = 9 files; selecting one region keeps 3, prunes 6.
    assert result.metrics["files_pruned"] == 6
    assert result.metrics["files_scanned"] == 3


def test_row_group_pruning_skips_row_groups(trips_dataset):
    # trip_id is monotonic; a high threshold prunes most row-groups.
    result = run_sql(
        "SELECT COUNT(*) AS n FROM trips WHERE trip_id > 35000", trips_dataset, max_workers=1
    )
    assert result.metrics["row_groups_pruned"] > 0
    assert result.metrics["row_groups_scanned"] < result.metrics["total_row_groups"]


def test_column_pruning_reduces_bytes(trips_dataset):
    one_col = run_sql("SELECT SUM(price) AS s FROM trips", trips_dataset, max_workers=1)
    all_cols = run_sql("SELECT * FROM trips", trips_dataset, max_workers=1)
    assert one_col.metrics["bytes_scanned"] < all_cols.metrics["bytes_scanned"]


def test_count_star_reads_the_narrowest_column(trips_dataset, oracle):
    """A COUNT(*) references no column, so it should read the cheapest one.

    It still has to read *something* to know how many rows there are, but
    picking the first declared column (a wide monotonic `trip_id`) instead of
    the narrowest one costs an order of magnitude more I/O for the same answer.
    """
    sql = "SELECT COUNT(*) AS n FROM trips"
    count = run_sql(sql, trips_dataset, max_workers=1)
    assert_matches(count, oracle, sql)
    assert count.metrics["columns_projected"] == 1

    widest = run_sql("SELECT SUM(trip_id) AS s FROM trips", trips_dataset, max_workers=1)
    assert count.metrics["bytes_scanned"] < widest.metrics["bytes_scanned"]


def test_group_by_partition_column_reads_no_data_column(trips_dataset, oracle):
    """`region` lives in the path, so grouping on it needs no data column."""
    sql = "SELECT region, COUNT(*) AS n FROM trips GROUP BY region"
    result = run_sql(sql, trips_dataset, max_workers=1)
    assert_matches(result, oracle, sql)
    assert result.metrics["columns_projected"] == 1
    assert "row counts only" not in result.plan_text  # resolved by now
