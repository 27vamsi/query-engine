from django.conf import settings
from django.db import models

from apps.catalog.models import Dataset


class QueryRun(models.Model):
    """One submitted SQL query and its execution record.

    Metrics (bytes_scanned, row_groups_pruned, ...) are the whole point of the
    history view: they make the pushdown/pruning wins visible over time.
    """

    class Status(models.TextChoices):
        PENDING = "pending"
        RUNNING = "running"
        SUCCESS = "success"
        ERROR = "error"

    dataset = models.ForeignKey(Dataset, on_delete=models.CASCADE, related_name="runs")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="query_runs",
    )
    sql = models.TextField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)

    plan_text = models.TextField(blank=True)
    plan_mermaid = models.TextField(blank=True)

    # Metrics
    duration_ms = models.FloatField(null=True, blank=True)
    bytes_scanned = models.BigIntegerField(null=True, blank=True)
    bytes_total = models.BigIntegerField(null=True, blank=True)
    rows_scanned = models.BigIntegerField(null=True, blank=True)
    rows_returned = models.IntegerField(null=True, blank=True)
    files_pruned = models.IntegerField(null=True, blank=True)
    row_groups_pruned = models.IntegerField(null=True, blank=True)
    metrics = models.JSONField(default=dict, blank=True)

    result_columns = models.JSONField(default=list, blank=True)
    result_preview = models.JSONField(default=list, blank=True)  # first N rows
    error = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Run #{self.pk} [{self.status}] {self.sql[:40]}"

    @property
    def scan_ratio(self) -> float | None:
        if self.bytes_total:
            return round(100 * (self.bytes_scanned or 0) / self.bytes_total, 1)
        return None

    # A tile per row-group gets unreadable, and the JSON gets big, long before
    # this. Past the cap the heatmap is dropped rather than truncated, so it
    # never shows a partial picture that looks like the whole one.
    HEATMAP_FILE_LIMIT = 64

    @property
    def scan_map(self) -> list[dict]:
        rows = (self.metrics or {}).get("scan_map") or []
        return rows if len(rows) <= self.HEATMAP_FILE_LIMIT else []

    @property
    def scan_stages(self) -> list[dict]:
        """The byte total broken into where it went, for the waterfall.

        The three savings plus what was actually read sum to bytes_total
        exactly, so the bars are a real decomposition rather than four
        independently-computed numbers that happen to look related.
        """
        m = self.metrics or {}
        total = m.get("bytes_total") or 0
        if not total:
            return []

        def stage(label, value, why, kind):
            return {
                "label": label,
                "bytes": value,
                "why": why,
                "kind": kind,
                "pct": round(100 * value / total, 1),
            }

        stages = [
            stage(
                "A naive full scan",
                total,
                "every byte of every column in every file",
                "total",
            )
        ]
        saved = [
            (
                "Files never opened",
                m.get("bytes_pruned_files") or 0,
                "partition values ruled them out from the path; zero I/O",
            ),
            (
                "Row-groups skipped",
                m.get("bytes_pruned_row_groups") or 0,
                "footer min/max stats proved they held no match",
            ),
            (
                "Columns not referenced",
                m.get("bytes_unprojected") or 0,
                "Parquet is columnar, so these were never touched",
            ),
        ]
        stages += [stage(label, value, why, "saved") for label, value, why in saved if value]
        stages.append(
            stage(
                "Actually read",
                m.get("bytes_scanned") or 0,
                "what came off disk to answer the query",
                "read",
            )
        )
        return stages

    @property
    def pruning_notes(self) -> list[str]:
        """The metrics restated in English, for the results page.

        Raw counters only mean something to someone who already knows how the
        scan path works; these lines are what a visitor actually reads.
        """
        m = self.metrics or {}
        notes: list[str] = []

        files_pruned = m.get("files_pruned") or 0
        total_files = m.get("total_files") or 0
        if files_pruned:
            notes.append(
                f"{files_pruned} of {total_files} files were never opened — the partition "
                f"filter ruled them out from the directory path alone, costing zero I/O."
            )

        groups_pruned = m.get("row_groups_pruned") or 0
        total_groups = m.get("total_row_groups") or 0
        if groups_pruned:
            notes.append(
                f"{groups_pruned} of {total_groups} row-groups were skipped — the min/max "
                f"statistics in the Parquet footers proved they could not contain a match."
            )

        projected = m.get("columns_projected") or 0
        total_columns = m.get("columns_total") or 0
        if projected and total_columns and projected < total_columns:
            notes.append(
                f"Only {projected} of {total_columns} columns came off disk — the other "
                f"{total_columns - projected} were never referenced, and Parquet is columnar."
            )

        if not notes:
            notes.append(
                "Nothing was pruned: this query had no filter the engine could push down, "
                "so every file, row-group and column had to be read. This is the baseline "
                "the other demo queries are measured against."
            )
        return notes
