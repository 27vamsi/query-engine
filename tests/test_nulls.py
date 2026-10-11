"""NULL semantics, checked against DuckDB.

The engine reconstructs AVG from partial sums and counts so it can aggregate in
parallel. That reconstruction is where SQL's NULL rules are easiest to lose: a
group whose every value is NULL has a count of 0, and sum/count becomes 0/0.
These tests pin the behaviour to what a real engine does.
"""

from apps.engine.api import run_sql

from .conftest import assert_matches


def test_avg_of_all_null_column_is_null(trips_dataset, oracle):
    sql = "SELECT AVG(surcharge) AS a FROM trips"
    r = run_sql(sql, trips_dataset, max_workers=1)
    assert r.rows[0]["a"] is None, f"expected NULL, got {r.rows[0]['a']!r}"
    assert_matches(r, oracle, sql)


def test_avg_of_all_null_group_is_null(trips_dataset, oracle):
    """`tip` is NULL for every wallet payment, so that group must come back NULL."""
    sql = "SELECT payment, AVG(tip) AS a FROM trips GROUP BY payment"
    r = run_sql(sql, trips_dataset, max_workers=1)
    wallet = [row for row in r.rows if row["payment"] == "wallet"]
    assert wallet, "fixture should contain a wallet group"
    assert wallet[0]["a"] is None, f"expected NULL, got {wallet[0]['a']!r}"
    assert_matches(r, oracle, sql)


def test_avg_all_null_group_survives_parallel_merge(trips_dataset, oracle):
    """Each worker contributes a zero count; merging must still yield NULL."""
    sql = "SELECT payment, AVG(tip) AS a FROM trips GROUP BY payment"
    serial = run_sql(sql, trips_dataset, max_workers=1)
    parallel = run_sql(sql, trips_dataset, max_workers=4)

    def key(rows):
        return sorted(((r["payment"], r["a"]) for r in rows), key=repr)

    assert key(serial.rows) == key(parallel.rows)
    assert_matches(parallel, oracle, sql)


def test_sum_of_all_null_group_is_null(trips_dataset, oracle):
    """SUM over no non-null values is NULL, not 0 -- polars returns 0 for both."""
    sql = "SELECT payment, SUM(tip) AS s FROM trips GROUP BY payment"
    r = run_sql(sql, trips_dataset, max_workers=1)
    wallet = [row for row in r.rows if row["payment"] == "wallet"]
    assert wallet[0]["s"] is None, f"expected NULL, got {wallet[0]['s']!r}"
    assert_matches(r, oracle, sql)


def test_sum_of_all_null_column_is_null(trips_dataset, oracle):
    sql = "SELECT SUM(surcharge) AS s FROM trips"
    r = run_sql(sql, trips_dataset, max_workers=1)
    assert r.rows[0]["s"] is None, f"expected NULL, got {r.rows[0]['s']!r}"
    assert_matches(r, oracle, sql)


def test_aggregates_ignore_nulls(trips_dataset, oracle):
    sql = (
        "SELECT COUNT(*) AS n, COUNT(tip) AS nt, SUM(tip) AS s, "
        "AVG(tip) AS a, MIN(tip) AS mn, MAX(tip) AS mx FROM trips"
    )
    assert_matches(run_sql(sql, trips_dataset, max_workers=1), oracle, sql)


def test_group_by_nullable_key(trips_dataset, oracle):
    sql = "SELECT payment, COUNT(*) AS n, SUM(tip) AS s FROM trips GROUP BY payment"
    assert_matches(run_sql(sql, trips_dataset, max_workers=1), oracle, sql)


def test_filter_on_nullable_column_excludes_nulls(trips_dataset, oracle):
    sql = "SELECT COUNT(*) AS n FROM trips WHERE tip > 5"
    assert_matches(run_sql(sql, trips_dataset, max_workers=1), oracle, sql)


def test_full_scan_reads_everything(trips_dataset):
    """The invariant that catches byte-accounting drift.

    bytes_scanned and bytes_total must be the same unit and the denominator must
    include pruned row-groups, so a query with nothing to prune reads exactly
    100% -- no more (double counting) and no less (mismatched units).
    """
    m = run_sql("SELECT * FROM trips", trips_dataset, max_workers=1).metrics
    assert m["files_pruned"] == 0
    assert m["row_groups_pruned"] == 0
    assert m["bytes_scanned"] == m["bytes_total"]
    assert m["columns_projected"] == m["columns_total"]


BYTE_BREAKDOWN_QUERIES = [
    "SELECT * FROM trips",
    "SELECT COUNT(*) AS n FROM trips",
    "SELECT COUNT(*) AS n FROM trips WHERE region = 'EU'",
    "SELECT COUNT(*) AS n FROM trips WHERE trip_id > 35000",
    "SELECT payment, SUM(price) AS s FROM trips WHERE region = 'EU' AND trip_id > 35000 GROUP BY payment",
]


def test_byte_breakdown_is_a_real_decomposition(trips_dataset):
    """The UI waterfall claims the savings add up to the full scan. Prove it.

    Four independently-accumulated counters are easy to get subtly wrong (one
    double-counts, one misses an edge case) and the resulting chart still looks
    plausible. Asserting the identity is what stops that.
    """
    for sql in BYTE_BREAKDOWN_QUERIES:
        m = run_sql(sql, trips_dataset, max_workers=1).metrics
        parts = (
            m["bytes_pruned_files"]
            + m["bytes_pruned_row_groups"]
            + m["bytes_unprojected"]
            + m["bytes_scanned"]
        )
        assert parts == m["bytes_total"], sql


def test_scan_map_mirrors_the_counters(trips_dataset):
    """The heatmap is drawn from scan_map, so it has to agree with the numbers
    printed next to it -- a tile per row-group, a pruned file with no tiles."""
    sql = "SELECT COUNT(*) AS n FROM trips WHERE region = 'EU' AND trip_id > 35000"
    m = run_sql(sql, trips_dataset, max_workers=1).metrics
    scan_map = m["scan_map"]

    assert len(scan_map) == m["total_files"]
    assert sum(1 for f in scan_map if f["pruned"]) == m["files_pruned"]
    # A pruned file's footer is never read, so it cannot report row-groups.
    assert all(f["groups"] == [] for f in scan_map if f["pruned"])

    tiles = [flag for f in scan_map for flag in f["groups"]]
    assert len(tiles) == m["total_row_groups"]
    assert tiles.count(True) == m["row_groups_scanned"]
    assert tiles.count(False) == m["row_groups_pruned"]
