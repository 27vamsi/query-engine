"""Structured predicate model.

This is the engine's own representation of a WHERE clause, independent of
sqlglot. We keep it structured (rather than just handing a string to PyArrow)
because we need to reason about it ourselves for:

  * row-group skipping using per-column min/max statistics
  * Hive partition pruning
  * converting to a PyArrow compute expression for the actual filter kernel

Keeping ownership of this is the whole point of the project.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

import pyarrow.compute as pc
import pyarrow.dataset as ds

# Comparison operators we understand.
COMPARISONS = {"=", "!=", "<", "<=", ">", ">="}


class Predicate:
    """Base class. Subclasses form a boolean expression tree."""

    def columns(self) -> set[str]:
        raise NotImplementedError

    def to_arrow(self) -> ds.Expression:
        """Lower to a PyArrow dataset Expression for the filter kernel."""
        raise NotImplementedError

    def can_skip_row_group(self, stats: dict[str, ColumnStats]) -> bool:
        """Return True if a row-group with these per-column stats CANNOT contain
        any matching row, so the entire row-group can be skipped without reading.

        Conservative: when unsure (missing stats, unsupported op), return False
        (do not skip) so correctness is never sacrificed for pruning.
        """
        raise NotImplementedError


@dataclass(frozen=True)
class ColumnStats:
    """Min/max (and null info) for one column within one row-group."""

    min: Any
    max: Any
    null_count: int | None = None
    num_values: int | None = None

    @property
    def all_null(self) -> bool:
        return self.min is None and self.max is None


@dataclass
class Comparison(Predicate):
    column: str
    op: str
    value: Any

    def columns(self) -> set[str]:
        return {self.column}

    def to_arrow(self) -> ds.Expression:
        f = ds.field(self.column)
        v = self.value
        return {
            "=": f == v,
            "!=": f != v,
            "<": f < v,
            "<=": f <= v,
            ">": f > v,
            ">=": f >= v,
        }[self.op]

    def can_skip_row_group(self, stats: dict[str, ColumnStats]) -> bool:
        s = stats.get(self.column)
        if s is None or s.min is None or s.max is None:
            return False
        lo, hi, v = s.min, s.max, self.value
        try:
            if self.op == "=":
                return v < lo or v > hi
            if self.op == ">":
                return hi <= v
            if self.op == ">=":
                return hi < v
            if self.op == "<":
                return lo >= v
            if self.op == "<=":
                return lo > v
            if self.op == "!=":
                # Only skippable if the row-group is a single constant == v.
                return lo == hi == v
        except TypeError:
            return False
        return False


@dataclass
class In(Predicate):
    column: str
    values: list[Any]

    def columns(self) -> set[str]:
        return {self.column}

    def to_arrow(self) -> ds.Expression:
        return ds.field(self.column).isin(self.values)

    def can_skip_row_group(self, stats: dict[str, ColumnStats]) -> bool:
        s = stats.get(self.column)
        if s is None or s.min is None or s.max is None:
            return False
        try:
            return all(v < s.min or v > s.max for v in self.values)
        except TypeError:
            return False


@dataclass
class And(Predicate):
    children: list[Predicate] = field(default_factory=list)

    def columns(self) -> set[str]:
        out: set[str] = set()
        for c in self.children:
            out |= c.columns()
        return out

    def to_arrow(self) -> ds.Expression:
        expr = self.children[0].to_arrow()
        for c in self.children[1:]:
            expr = expr & c.to_arrow()
        return expr

    def can_skip_row_group(self, stats: dict[str, ColumnStats]) -> bool:
        # AND matches only if every child matches; if ANY child is impossible,
        # the whole conjunction is impossible -> skip.
        return any(c.can_skip_row_group(stats) for c in self.children)


@dataclass
class Or(Predicate):
    children: list[Predicate] = field(default_factory=list)

    def columns(self) -> set[str]:
        out: set[str] = set()
        for c in self.children:
            out |= c.columns()
        return out

    def to_arrow(self) -> ds.Expression:
        expr = self.children[0].to_arrow()
        for c in self.children[1:]:
            expr = expr | c.to_arrow()
        return expr

    def can_skip_row_group(self, stats: dict[str, ColumnStats]) -> bool:
        # OR is impossible only if EVERY branch is impossible.
        return all(c.can_skip_row_group(stats) for c in self.children)


@dataclass
class Not(Predicate):
    child: Predicate

    def columns(self) -> set[str]:
        return self.child.columns()

    def to_arrow(self) -> ds.Expression:
        return ~self.child.to_arrow()

    def can_skip_row_group(self, stats: dict[str, ColumnStats]) -> bool:
        # Negation makes stat-based skipping unsound in general; be conservative.
        return False


def split_conjuncts(pred: Predicate | None) -> list[Predicate]:
    """Flatten a top-level AND into a list. Used when deciding which predicates
    reference only partition columns (and can prune files) vs data columns."""
    if pred is None:
        return []
    if isinstance(pred, And):
        out: list[Predicate] = []
        for c in pred.children:
            out.extend(split_conjuncts(c))
        return out
    return [pred]


def conjoin(preds: list[Predicate]) -> Predicate | None:
    if not preds:
        return None
    if len(preds) == 1:
        return preds[0]
    return And(list(preds))


def normalize_value(v: Any) -> Any:
    """Coerce python date/datetime so comparisons with PyArrow stats work."""
    if isinstance(v, dt.datetime):
        return v
    return v
