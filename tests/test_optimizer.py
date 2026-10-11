from apps.engine.logical_plan import Filter, find_scan, walk
from apps.engine.optimizer import OptimizerContext, optimize
from apps.engine.parser import parse_sql
from apps.engine.predicates import ColumnStats

DATA_COLS = ["trip_id", "price", "distance", "payment"]
PART_COLS = ["region", "date"]
CTX = OptimizerContext(all_columns=DATA_COLS + PART_COLS, partition_columns=PART_COLS)


def _opt(sql):
    return optimize(parse_sql(sql), CTX)


def test_projection_pushdown_reads_only_referenced_columns():
    scan = find_scan(_opt("SELECT region, SUM(price) AS r FROM ds GROUP BY region"))
    # region is a partition column (not read from data); price is read.
    assert scan.projection == ["price"]


def test_predicate_split_partition_vs_data():
    scan = find_scan(_opt("SELECT price FROM ds WHERE region = 'EU' AND price > 50"))
    assert scan.partition_filter is not None
    assert scan.partition_filter.columns() == {"region"}
    assert scan.pushed_filter is not None
    assert scan.pushed_filter.columns() == {"price"}


def test_filter_node_removed_after_pushdown():
    plan = _opt("SELECT price FROM ds WHERE price > 50")
    assert not [n for n in walk(plan) if isinstance(n, Filter)]


def test_count_star_references_no_data_column():
    """`[]` means "nothing referenced", which is not the same as `None` ("all").

    Physical planning turns the empty projection into the narrowest column in
    the footer; the optimizer must not collapse it to a full scan.
    """
    scan = find_scan(_opt("SELECT COUNT(*) AS n FROM ds"))
    assert scan.projection == []


def test_row_group_skip_decisions():
    # price > 100 against a row-group whose max price is 90 -> skippable
    scan = find_scan(_opt("SELECT price FROM ds WHERE price > 100"))
    low = {"price": ColumnStats(min=10.0, max=90.0)}
    high = {"price": ColumnStats(min=120.0, max=200.0)}
    assert scan.pushed_filter.can_skip_row_group(low) is True
    assert scan.pushed_filter.can_skip_row_group(high) is False


def test_conjunction_skip_if_any_branch_impossible():
    scan = find_scan(_opt("SELECT price FROM ds WHERE price > 100 AND distance < 5"))
    stats = {
        "price": ColumnStats(min=120.0, max=200.0),  # ok
        "distance": ColumnStats(min=10.0, max=40.0),  # distance<5 impossible
    }
    assert scan.pushed_filter.can_skip_row_group(stats) is True
