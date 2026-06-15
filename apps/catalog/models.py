from django.conf import settings
from django.db import models


class Dataset(models.Model):
    """A registered collection of Parquet files at a storage URI.

    `uri` is anything the engine's resolve_fs understands:
        ./data/trips            (local)
        s3://bucket/prefix      (S3/MinIO)
    """

    name = models.SlugField(max_length=128, unique=True)
    uri = models.CharField(max_length=512)
    description = models.TextField(blank=True)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="datasets",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    # Populated by the schema-inference service when the dataset is registered.
    file_count = models.IntegerField(default=0)
    total_bytes = models.BigIntegerField(default=0)

    def __str__(self) -> str:
        return self.name

    @property
    def partition_columns(self) -> list[str]:
        return list(
            self.columns.filter(is_partition=True)
            .order_by("position")
            .values_list("name", flat=True)
        )

    @property
    def column_names(self) -> list[str]:
        return list(self.columns.order_by("position").values_list("name", flat=True))


class Column(models.Model):
    dataset = models.ForeignKey(Dataset, on_delete=models.CASCADE, related_name="columns")
    name = models.CharField(max_length=128)
    arrow_type = models.CharField(max_length=64, blank=True)
    is_partition = models.BooleanField(default=False)
    position = models.IntegerField(default=0)

    class Meta:
        unique_together = ("dataset", "name")
        ordering = ["position"]

    def __str__(self) -> str:
        flag = " (partition)" if self.is_partition else ""
        return f"{self.dataset.name}.{self.name}{flag}"


class Partition(models.Model):
    """One discovered Hive partition value combination + its file path."""

    dataset = models.ForeignKey(Dataset, on_delete=models.CASCADE, related_name="partitions")
    path = models.CharField(max_length=1024)
    values = models.JSONField(default=dict)
    size_bytes = models.BigIntegerField(default=0)

    class Meta:
        unique_together = ("dataset", "path")

    def __str__(self) -> str:
        return f"{self.dataset.name}: {self.values}"
