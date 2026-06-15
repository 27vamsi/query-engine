from django.contrib import admin

from .models import Column, Dataset, Partition


class ColumnInline(admin.TabularInline):
    model = Column
    extra = 0


@admin.register(Dataset)
class DatasetAdmin(admin.ModelAdmin):
    list_display = ("name", "uri", "file_count", "total_bytes", "created_at")
    search_fields = ("name", "uri")
    inlines = [ColumnInline]


@admin.register(Partition)
class PartitionAdmin(admin.ModelAdmin):
    list_display = ("dataset", "path", "size_bytes")
    list_filter = ("dataset",)
    search_fields = ("path",)
