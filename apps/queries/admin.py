from django.contrib import admin

from .models import QueryRun


@admin.register(QueryRun)
class QueryRunAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "dataset",
        "status",
        "duration_ms",
        "bytes_scanned",
        "row_groups_pruned",
        "files_pruned",
        "created_at",
    )
    list_filter = ("status", "dataset")
    search_fields = ("sql",)
    readonly_fields = (
        "plan_text",
        "plan_mermaid",
        "metrics",
        "result_columns",
        "result_preview",
        "created_at",
        "finished_at",
    )
