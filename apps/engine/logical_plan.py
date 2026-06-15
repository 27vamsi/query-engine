"""Logical plan nodes.

A query lowers to a tree of these. Canonical shape produced by the parser:

    Limit
      Sort
        Aggregate
          Project
            Filter
              Scan

Not every node is always present. The optimizer rewrites this tree (pushing
predicates and the projection into Scan) but the node types stay the same.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .predicates import Predicate


class LogicalNode:
    children: list["LogicalNode"]

    def describe(self) -> str:
        raise NotImplementedError


@dataclass
class Scan(LogicalNode):
    dataset: str
    # Filled in by the optimizer:
    projection: list[str] | None = None  # columns to actually read (None = all)
    pushed_filter: Predicate | None = None  # data-column predicate for row-groups
    partition_filter: Predicate | None = None  # partition-column predicate for files

    children: list[LogicalNode] = field(default_factory=list)

    def describe(self) -> str:
        cols = ",".join(self.projection) if self.projection else "*"
        parts = [f"Scan {self.dataset} cols=[{cols}]"]
        if self.partition_filter:
            parts.append("part_filter")
        if self.pushed_filter:
            parts.append("pushdown")
        return " ".join(parts)


@dataclass
class Filter(LogicalNode):
    predicate: Predicate
    children: list[LogicalNode] = field(default_factory=list)

    @property
    def input(self) -> LogicalNode:
        return self.children[0]

    def describe(self) -> str:
        return "Filter"


@dataclass
class Project(LogicalNode):
    # (output_name, source_expression_sql) — expression is a sqlglot string we
    # evaluate at the end. For plain columns source == name.
    columns: list[tuple[str, str]]
    children: list[LogicalNode] = field(default_factory=list)

    def describe(self) -> str:
        return "Project " + ",".join(name for name, _ in self.columns)


@dataclass
class AggregateExpr:
    func: str  # count, sum, avg, min, max
    column: str | None  # None for count(*)
    output: str


@dataclass
class Aggregate(LogicalNode):
    group_by: list[str]
    aggregates: list[AggregateExpr]
    output_columns: list[str] = field(default_factory=list)  # final col order (SELECT)
    children: list[LogicalNode] = field(default_factory=list)

    def describe(self) -> str:
        g = ",".join(self.group_by) if self.group_by else "-"
        a = ",".join(f"{x.func}({x.column or '*'})" for x in self.aggregates)
        return f"Aggregate group=[{g}] aggs=[{a}]"


@dataclass
class Sort(LogicalNode):
    keys: list[tuple[str, bool]]  # (column, descending)
    children: list[LogicalNode] = field(default_factory=list)

    def describe(self) -> str:
        return "Sort " + ",".join(
            f"{c}{' desc' if d else ''}" for c, d in self.keys
        )


@dataclass
class Limit(LogicalNode):
    n: int
    children: list[LogicalNode] = field(default_factory=list)

    def describe(self) -> str:
        return f"Limit {self.n}"


def walk(node: LogicalNode):
    """Pre-order traversal."""
    yield node
    for child in node.children:
        yield from walk(child)


def find_scan(node: LogicalNode) -> Scan:
    for n in walk(node):
        if isinstance(n, Scan):
            return n
    raise ValueError("plan has no Scan node")


def format_plan(node: LogicalNode, indent: int = 0) -> str:
    """Human-readable indented plan tree."""
    line = "  " * indent + node.describe()
    out = [line]
    for child in node.children:
        out.append(format_plan(child, indent + 1))
    return "\n".join(out)


def to_mermaid(node: LogicalNode) -> str:
    """Render the plan as a Mermaid top-down graph for the UI."""
    lines = ["graph TD"]
    counter = [0]

    def visit(n: LogicalNode) -> str:
        nid = f"n{counter[0]}"
        counter[0] += 1
        label = n.describe().replace('"', "'")
        lines.append(f'    {nid}["{label}"]')
        for child in n.children:
            cid = visit(child)
            lines.append(f"    {nid} --> {cid}")
        return nid

    visit(node)
    return "\n".join(lines)
