from apps.engine.logical_plan import Aggregate, Filter, Limit, Project, Scan, Sort, walk
from apps.engine.parser import ParseError, parse_sql
from apps.engine.predicates import And, Comparison, In
import pytest


def _nodes(plan, cls):
    return [n for n in walk(plan) if isinstance(n, cls)]


def test_simple_projection():
    plan = parse_sql("SELECT a, b FROM ds")
    assert _nodes(plan, Scan)
    proj = _nodes(plan, Project)[0]
    assert [name for name, _ in proj.columns] == ["a", "b"]


def test_select_star_has_no_project():
    plan = parse_sql("SELECT * FROM ds")
    assert not _nodes(plan, Project)


def test_where_builds_filter_and_predicate():
    plan = parse_sql("SELECT a FROM ds WHERE x > 5 AND region = 'EU'")
    filt = _nodes(plan, Filter)[0]
    assert isinstance(filt.predicate, And)
    cols = filt.predicate.columns()
    assert cols == {"x", "region"}


def test_in_and_between():
    plan = parse_sql("SELECT a FROM ds WHERE region IN ('EU','US') AND x BETWEEN 1 AND 9")
    filt = _nodes(plan, Filter)[0]
    preds = filt.predicate.children
    assert any(isinstance(p, In) for p in preds)


def test_aggregate_plan():
    plan = parse_sql(
        "SELECT region, SUM(price) AS rev, COUNT(*) AS n FROM ds GROUP BY region"
    )
    agg = _nodes(plan, Aggregate)[0]
    assert agg.group_by == ["region"]
    funcs = {a.func for a in agg.aggregates}
    assert funcs == {"sum", "count"}
    assert agg.output_columns == ["region", "rev", "n"]


def test_order_and_limit():
    plan = parse_sql("SELECT a FROM ds ORDER BY a DESC LIMIT 10")
    assert _nodes(plan, Sort)[0].keys == [("a", True)]
    assert _nodes(plan, Limit)[0].n == 10


def test_rejects_non_select():
    with pytest.raises(ParseError):
        parse_sql("DELETE FROM ds")
