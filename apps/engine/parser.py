"""SQL -> logical plan, using sqlglot for parsing.

We support a teaching-grade subset of analytical SQL:

    SELECT <cols | aggregates>
    FROM <dataset>
    [WHERE <predicate>]
    [GROUP BY <cols>]
    [ORDER BY <cols>]
    [LIMIT <n>]

Joins and subqueries are intentionally out of scope (the depth here is in the
scan/pushdown/parallel-execution path, not the relational algebra breadth).
"""

from __future__ import annotations

import datetime as dt

import sqlglot
from sqlglot import exp

from .logical_plan import (
    Aggregate,
    AggregateExpr,
    Filter,
    Limit,
    LogicalNode,
    Project,
    Scan,
    Sort,
)
from .predicates import And, Comparison, In, Not, Or, Predicate

AGG_FUNCS = {"count", "sum", "avg", "min", "max"}


class ParseError(ValueError):
    pass


def parse_sql(sql: str) -> LogicalNode:
    try:
        tree = sqlglot.parse_one(sql, read="duckdb")
    except Exception as e:  # noqa: BLE001 - surface a clean error to the API
        raise ParseError(f"could not parse SQL: {e}") from e

    if not isinstance(tree, exp.Select):
        raise ParseError("only SELECT statements are supported")

    dataset = _extract_dataset(tree)

    # Bottom-up: Scan -> Filter -> Aggregate/Project -> Sort -> Limit
    node: LogicalNode = Scan(dataset=dataset)

    where = tree.args.get("where")
    if where is not None:
        predicate = _to_predicate(where.this)
        node = Filter(predicate=predicate, children=[node])

    group = tree.args.get("group")
    select_exprs = tree.expressions

    if group is not None or _has_aggregate(select_exprs):
        node = _build_aggregate(select_exprs, group, node)
    else:
        node = _build_project(select_exprs, node)

    order = tree.args.get("order")
    if order is not None:
        keys = []
        for o in order.expressions:
            col = _col_name(o.this)
            desc = bool(o.args.get("desc"))
            keys.append((col, desc))
        node = Sort(keys=keys, children=[node])

    limit = tree.args.get("limit")
    if limit is not None:
        n = int(limit.expression.this)
        node = Limit(n=n, children=[node])

    return node


def _extract_dataset(select: exp.Select) -> str:
    from_ = select.args.get("from") or select.args.get("from_")
    if from_ is None:
        raise ParseError("query has no FROM clause")
    table = from_.this
    if not isinstance(table, exp.Table):
        raise ParseError("FROM must reference a single dataset")
    return table.name


def _has_aggregate(exprs: list[exp.Expression]) -> bool:
    for e in exprs:
        for node in e.walk():
            if isinstance(node, exp.AggFunc):
                return True
    return False


def _build_aggregate(
    select_exprs: list[exp.Expression],
    group: exp.Group | None,
    child: LogicalNode,
) -> Aggregate:
    group_cols = []
    if group is not None:
        group_cols = [_col_name(g) for g in group.expressions]

    aggregates: list[AggregateExpr] = []
    group_seen: list[str] = []
    output_columns: list[str] = []

    for e in select_exprs:
        target = e.this if isinstance(e, exp.Alias) else e
        alias = e.alias_or_name

        if isinstance(target, exp.AggFunc):
            func = target.sql_name().lower()
            if func not in AGG_FUNCS:
                raise ParseError(f"unsupported aggregate: {func}")
            arg = target.this
            if func == "count" and (arg is None or isinstance(arg, exp.Star)):
                col = None
            else:
                col = _col_name(arg)
            output = alias if isinstance(e, exp.Alias) else _default_agg_name(func, col)
            aggregates.append(AggregateExpr(func=func, column=col, output=output))
            output_columns.append(output)
        elif isinstance(target, exp.Column):
            name = _col_name(target)
            group_seen.append(name)
            output_columns.append(name)
        elif isinstance(target, exp.Star):
            raise ParseError("SELECT * with GROUP BY is not supported")
        else:
            raise ParseError(f"unsupported select expression: {target.sql()}")

    # group_by columns: prefer GROUP BY clause, fall back to plain select cols
    final_group = group_cols or group_seen
    return Aggregate(
        group_by=final_group,
        aggregates=aggregates,
        output_columns=output_columns,
        children=[child],
    )


def _default_agg_name(func: str, col: str | None) -> str:
    return f"{func}_{col}" if col else f"{func}_star"


def _build_project(
    select_exprs: list[exp.Expression], child: LogicalNode
) -> LogicalNode:
    # SELECT * -> no projection node; let executor return all columns.
    if len(select_exprs) == 1 and isinstance(select_exprs[0], exp.Star):
        return child
    columns: list[tuple[str, str]] = []
    for e in select_exprs:
        target = e.this if isinstance(e, exp.Alias) else e
        if isinstance(target, exp.Column):
            name = e.alias_or_name
            columns.append((name, _col_name(target)))
        elif isinstance(target, exp.Star):
            raise ParseError("cannot mix * with explicit columns")
        else:
            # scalar expression (e.g. price * qty) -> keep its SQL, evaluate later
            name = e.alias_or_name
            columns.append((name, target.sql(dialect="duckdb")))
    return Project(columns=columns, children=[child])


# --------------------------------------------------------------------------
# WHERE clause -> Predicate tree
# --------------------------------------------------------------------------

_OP_MAP = {
    exp.EQ: "=",
    exp.NEQ: "!=",
    exp.LT: "<",
    exp.LTE: "<=",
    exp.GT: ">",
    exp.GTE: ">=",
}


def _to_predicate(node: exp.Expression) -> Predicate:
    if isinstance(node, exp.And):
        return And([_to_predicate(node.left), _to_predicate(node.right)])
    if isinstance(node, exp.Or):
        return Or([_to_predicate(node.left), _to_predicate(node.right)])
    if isinstance(node, exp.Not):
        return Not(_to_predicate(node.this))
    if isinstance(node, exp.Paren):
        return _to_predicate(node.this)
    if isinstance(node, tuple(_OP_MAP)):
        op = _OP_MAP[type(node)]
        col, val = _column_and_literal(node)
        return Comparison(column=col, op=op, value=val)
    if isinstance(node, exp.In):
        col = _col_name(node.this)
        values = [_literal_value(v) for v in node.expressions]
        return In(column=col, values=values)
    if isinstance(node, exp.Between):
        col = _col_name(node.this)
        low = _literal_value(node.args["low"])
        high = _literal_value(node.args["high"])
        return And(
            [
                Comparison(col, ">=", low),
                Comparison(col, "<=", high),
            ]
        )
    raise ParseError(f"unsupported WHERE expression: {node.sql()}")


def _column_and_literal(node: exp.Binary) -> tuple[str, object]:
    left, right = node.left, node.right
    if isinstance(left, exp.Column):
        return _col_name(left), _literal_value(right)
    if isinstance(right, exp.Column):
        # flip is handled by caller op; for simplicity require column on left
        return _col_name(right), _literal_value(left)
    raise ParseError(f"comparison must reference a column: {node.sql()}")


def _col_name(node: exp.Expression) -> str:
    if isinstance(node, exp.Column):
        return node.name
    if isinstance(node, exp.Alias):
        return node.alias_or_name
    raise ParseError(f"expected a column, got: {node.sql()}")


def _literal_value(node: exp.Expression):
    if isinstance(node, exp.Literal):
        if node.is_string:
            return node.this
        text = node.this
        return float(text) if "." in text else int(text)
    if isinstance(node, exp.Boolean):
        return bool(node.this)
    if isinstance(node, exp.Null):
        return None
    if isinstance(node, exp.Cast):
        # e.g. CAST('2024-01-01' AS DATE)
        inner = _literal_value(node.this)
        if isinstance(inner, str):
            return _maybe_date(inner)
        return inner
    if isinstance(node, exp.Neg):
        return -_literal_value(node.this)
    raise ParseError(f"expected a literal, got: {node.sql()}")


def _maybe_date(s: str):
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return s
