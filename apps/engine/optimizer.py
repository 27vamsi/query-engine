"""Rule-based logical optimizer.

Three rewrites, all of which reduce I/O before any byte is read:

  1. Projection pushdown  -> Scan.projection
       Collect every column referenced anywhere in the plan and tell the Scan
       to read only those. This is the core columnar win.

  2. Predicate pushdown    -> Scan.pushed_filter
       Move data-column WHERE conjuncts into the Scan so the reader can skip
       row-groups via min/max stats and apply the filter kernel during read.

  3. Partition pruning     -> Scan.partition_filter
       WHERE conjuncts that reference ONLY Hive partition columns become a
       file-level filter, so we never even open non-matching files.

We need to know which columns are partition columns; the caller supplies that
(it comes from the catalog metadata). Conjuncts touching a mix of partition and
data columns stay as data predicates (safe: still applied during the scan).
"""

from __future__ import annotations

from dataclasses import dataclass

from .logical_plan import (
    Aggregate,
    Filter,
    Limit,
    LogicalNode,
    Project,
    Scan,
    Sort,
    find_scan,
    walk,
)
from .predicates import Predicate, conjoin, split_conjuncts


@dataclass
class OptimizerContext:
    all_columns: list[str]
    partition_columns: list[str]


def optimize(plan: LogicalNode, ctx: OptimizerContext) -> LogicalNode:
    plan = _pushdown_predicates(plan, ctx)
    _pushdown_projection(plan, ctx)
    return plan


def _pushdown_predicates(plan: LogicalNode, ctx: OptimizerContext) -> LogicalNode:
    scan = find_scan(plan)
    part_cols = set(ctx.partition_columns)

    # Find the Filter node sitting directly above the Scan (canonical shape).
    filt = _filter_above_scan(plan)
    if filt is None:
        return plan

    conjuncts = split_conjuncts(filt.predicate)
    partition_preds: list[Predicate] = []
    data_preds: list[Predicate] = []
    for c in conjuncts:
        cols = c.columns()
        if cols and cols <= part_cols:
            partition_preds.append(c)
        else:
            data_preds.append(c)

    scan.partition_filter = conjoin(partition_preds)
    scan.pushed_filter = conjoin(data_preds)

    # The Scan now enforces the full predicate (partition + data), so the
    # Filter node is redundant and can be removed from the tree.
    return _remove_node(plan, filt)


def _filter_above_scan(plan: LogicalNode) -> Filter | None:
    for n in walk(plan):
        if isinstance(n, Filter) and any(isinstance(c, Scan) for c in n.children):
            return n
    # also handle Filter not directly above scan but still the only filter
    for n in walk(plan):
        if isinstance(n, Filter):
            return n
    return None


def _remove_node(plan: LogicalNode, target: LogicalNode) -> LogicalNode:
    if plan is target:
        return target.children[0]
    for n in walk(plan):
        for i, child in enumerate(n.children):
            if child is target:
                n.children[i] = target.children[0]
                return plan
    return plan


def _pushdown_projection(plan: LogicalNode, ctx: OptimizerContext) -> None:
    scan = find_scan(plan)

    # SELECT * lowers to neither a Project nor an Aggregate node; it must read
    # every column. (COUNT(*) DOES produce an Aggregate, so it is excluded.)
    has_project = any(isinstance(n, Project) for n in walk(plan))
    has_aggregate = any(isinstance(n, Aggregate) for n in walk(plan))
    if not has_project and not has_aggregate:
        scan.projection = None
        return

    needed: set[str] = set()

    for n in walk(plan):
        if isinstance(n, Project):
            for _, src in n.columns:
                needed |= _columns_in_expr(src, ctx.all_columns)
        elif isinstance(n, Aggregate):
            needed.update(n.group_by)
            for a in n.aggregates:
                if a.column:
                    needed.add(a.column)
        elif isinstance(n, Sort):
            needed.update(k for k, _ in n.keys)
        elif isinstance(n, Filter):
            needed |= n.predicate.columns()

    if scan.pushed_filter is not None:
        needed |= scan.pushed_filter.columns()

    # Only project columns that physically exist in files (exclude partition
    # columns, which come from the path, not the row data).
    data_cols = [c for c in ctx.all_columns if c not in set(ctx.partition_columns)]
    projection = [c for c in data_cols if c in needed]

    # No referenced data columns (e.g. COUNT(*)) -> read the single cheapest
    # column so row counts are still correct but I/O stays minimal.
    if not projection:
        projection = data_cols[:1] if data_cols else None

    scan.projection = projection


def _columns_in_expr(expr_sql: str, all_columns: list[str]) -> set[str]:
    # expr_sql is either a bare column name or a scalar SQL expression. Match
    # against known column identifiers.
    import re

    tokens = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", expr_sql))
    return {c for c in all_columns if c in tokens}


def collect_referenced_columns(plan: LogicalNode, all_columns: list[str]) -> set[str]:
    """Used by tests/UI to show which columns the query actually touches."""
    ctx_cols = all_columns
    needed: set[str] = set()
    for n in walk(plan):
        if isinstance(n, Project):
            for _, src in n.columns:
                needed |= _columns_in_expr(src, ctx_cols)
        elif isinstance(n, Aggregate):
            needed.update(n.group_by)
            needed.update(a.column for a in n.aggregates if a.column)
        elif isinstance(n, Filter):
            needed |= n.predicate.columns()
        elif isinstance(n, Sort):
            needed.update(k for k, _ in n.keys)
        elif isinstance(n, (Limit,)):
            pass
    return needed
