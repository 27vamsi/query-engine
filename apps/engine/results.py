"""Streaming result helpers.

The executor produces rows; we never want to hand the whole result set to a
client at once. `batches` yields fixed-size chunks so the API/Celery layer can
stream them (NDJSON, SSE, websocket — caller's choice)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any


def batches(rows: list[dict[str, Any]], size: int = 1000) -> Iterator[list[dict]]:
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


def to_records(columns: list[str], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """JSON-serialisable envelope used by the DRF API."""
    return {"columns": columns, "rows": [_json_safe(r) for r in rows], "row_count": len(rows)}


def _json_safe(row: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k, v in row.items():
        out[k] = _coerce(v)
    return out


def _coerce(v: Any) -> Any:
    import datetime as dt
    import decimal

    if isinstance(v, (dt.date, dt.datetime, dt.time)):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return v
