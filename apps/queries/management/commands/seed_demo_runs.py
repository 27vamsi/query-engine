"""Populate query history by actually executing the demo queries.

A fresh deploy has no QueryRun rows, so Overview, History and Performance all
render empty states -- a poor first impression for a demo whose whole point is
showing measured I/O. This runs the real engine over the real dataset and
stores the real results, so every number on the deployed site is genuine. It
does not fabricate timings or metrics.

Idempotent by default: it does nothing if history already exists, so a redeploy
on a persistent database does not keep appending duplicates.

    python manage.py seed_demo_runs
    python manage.py seed_demo_runs --passes 3 --force
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.catalog.models import Dataset
from apps.catalog.services import preview_stats
from apps.frontend.demos import build_demo_queries
from apps.queries.models import QueryRun
from apps.queries.tasks import run_query


class Command(BaseCommand):
    help = "Execute the demo queries so the deployed site has real history."

    def add_arguments(self, parser):
        parser.add_argument(
            "--passes",
            type=int,
            default=2,
            help="How many times to run each demo query (default 2). More passes "
            "give the latency chart a visible warm/cold spread.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Seed even if query history already exists.",
        )

    def handle(self, *args, **opts):
        if QueryRun.objects.exists() and not opts["force"]:
            self.stdout.write("Query history already present; skipping (use --force).")
            return

        datasets = list(Dataset.objects.all())
        if not datasets:
            self.stdout.write(
                self.style.WARNING("No datasets registered; nothing to run.")
            )
            return

        ok = failed = 0
        for dataset in datasets:
            # build_demo_queries needs column stats to pick selective thresholds;
            # without them it just yields the queries that need no threshold.
            try:
                stats = preview_stats(dataset)
            except Exception:  # noqa: BLE001
                stats = {}
            demos = build_demo_queries(dataset, stats)
            for _ in range(max(1, opts["passes"])):
                for demo in demos:
                    run = QueryRun.objects.create(dataset=dataset, sql=demo.sql)
                    try:
                        run_query(run.id)
                    except Exception as exc:  # noqa: BLE001 - seeding must not abort
                        failed += 1
                        self.stderr.write(f"  {demo.title}: {exc}")
                        continue
                    run.refresh_from_db()
                    if run.status == QueryRun.Status.SUCCESS:
                        ok += 1
                        self.stdout.write(
                            f"  {demo.title}: {run.duration_ms} ms, "
                            f"{run.scan_ratio if run.scan_ratio is None else round(run.scan_ratio, 1)}% read"
                        )
                    else:
                        failed += 1
                        self.stderr.write(f"  {demo.title}: {run.error}")

        style = self.style.SUCCESS if ok and not failed else self.style.WARNING
        self.stdout.write(style(f"Seeded {ok} successful runs, {failed} failed."))
