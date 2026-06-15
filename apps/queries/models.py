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
