from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.routers import DefaultRouter

from apps.catalog.models import Dataset
from apps.engine.api import explain_sql
from apps.catalog.services import engine_schema

from .models import QueryRun
from .tasks import run_query


class QueryRunSerializer(serializers.ModelSerializer):
    scan_ratio = serializers.ReadOnlyField()
    dataset_name = serializers.CharField(source="dataset.name", read_only=True)

    class Meta:
        model = QueryRun
        fields = [
            "id",
            "dataset",
            "dataset_name",
            "sql",
            "status",
            "duration_ms",
            "bytes_scanned",
            "bytes_total",
            "scan_ratio",
            "rows_scanned",
            "rows_returned",
            "files_pruned",
            "row_groups_pruned",
            "metrics",
            "plan_text",
            "plan_mermaid",
            "result_columns",
            "result_preview",
            "error",
            "created_at",
            "finished_at",
        ]
        read_only_fields = [f for f in fields if f not in ("dataset", "sql")]


class QueryRunViewSet(viewsets.ModelViewSet):
    queryset = QueryRun.objects.select_related("dataset").all()
    serializer_class = QueryRunSerializer

    def create(self, request, *args, **kwargs):
        dataset_id = request.data.get("dataset")
        sql = request.data.get("sql")
        if not dataset_id or not sql:
            return Response({"detail": "dataset and sql are required"}, status=400)
        try:
            dataset = Dataset.objects.get(pk=dataset_id)
        except Dataset.DoesNotExist:
            return Response({"detail": "dataset not found"}, status=404)

        run = QueryRun.objects.create(dataset=dataset, sql=sql)
        run_query.delay(run.id)
        run.refresh_from_db()
        return Response(self.get_serializer(run).data, status=201)

    @action(detail=False, methods=["post"])
    def explain(self, request):
        """Return the optimized plan without executing."""
        dataset_id = request.data.get("dataset")
        sql = request.data.get("sql")
        try:
            dataset = Dataset.objects.get(pk=dataset_id)
        except Dataset.DoesNotExist:
            return Response({"detail": "dataset not found"}, status=404)
        try:
            out = explain_sql(sql, dataset.uri, schema=engine_schema(dataset))
        except Exception as e:  # noqa: BLE001
            return Response({"detail": str(e)}, status=400)
        return Response(out)


router = DefaultRouter()
router.register("runs", QueryRunViewSet)

urlpatterns = router.urls
