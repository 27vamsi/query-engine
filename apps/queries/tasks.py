"""Celery task that drives the engine for one QueryRun.

Long-running queries execute off the request thread; the client polls
`query_run_status` (htmx) for live updates. With no Redis broker configured the
task runs eagerly (inline), so the same code path works on a laptop.
"""

from __future__ import annotations

from django.conf import settings
from django.utils import timezone

from celery import shared_task

from apps.catalog.services import engine_schema
from apps.engine.api import run_sql

from .cache import cached_row_group_metadata
from .models import QueryRun

PREVIEW_ROWS = 200


@shared_task
def run_query(run_id: int) -> int:
    run = QueryRun.objects.select_related("dataset").get(pk=run_id)
    run.status = QueryRun.Status.RUNNING
    run.save(update_fields=["status"])

    try:
        schema = engine_schema(run.dataset)
        result = run_sql(
            sql=run.sql,
            dataset_uri=run.dataset.uri,
            schema=schema,
            max_workers=settings.ENGINE_MAX_WORKERS,
            metadata_fn=cached_row_group_metadata,
        )
    except Exception as e:  # noqa: BLE001 - record any failure for the user
        run.status = QueryRun.Status.ERROR
        run.error = f"{type(e).__name__}: {e}"
        run.finished_at = timezone.now()
        run.save()
        return run_id

    m = result.metrics
    run.status = QueryRun.Status.SUCCESS
    run.plan_text = result.plan_text
    run.plan_mermaid = result.plan_mermaid
    run.duration_ms = result.duration_ms
    run.bytes_scanned = m.get("bytes_scanned")
    run.bytes_total = m.get("bytes_total")
    run.rows_scanned = m.get("rows_scanned")
    run.files_pruned = m.get("files_pruned")
    run.row_groups_pruned = m.get("row_groups_pruned")
    run.metrics = m
    run.rows_returned = len(result.rows)
    run.result_columns = result.columns

    from apps.engine.results import to_records

    preview = to_records(result.columns, result.rows[:PREVIEW_ROWS])
    run.result_preview = preview["rows"]
    run.finished_at = timezone.now()
    run.save()
    return run_id
