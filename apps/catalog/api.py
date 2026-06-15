from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.routers import DefaultRouter

from .models import Column, Dataset
from .services import register_dataset


class ColumnSerializer(serializers.ModelSerializer):
    class Meta:
        model = Column
        fields = ["name", "arrow_type", "is_partition", "position"]


class DatasetSerializer(serializers.ModelSerializer):
    columns = ColumnSerializer(many=True, read_only=True)

    class Meta:
        model = Dataset
        fields = [
            "id",
            "name",
            "uri",
            "description",
            "file_count",
            "total_bytes",
            "created_at",
            "columns",
        ]


class DatasetViewSet(viewsets.ModelViewSet):
    queryset = Dataset.objects.all().prefetch_related("columns")
    serializer_class = DatasetSerializer

    def create(self, request, *args, **kwargs):
        """Register a dataset by introspecting its Parquet files."""
        name = request.data.get("name")
        uri = request.data.get("uri")
        description = request.data.get("description", "")
        if not name or not uri:
            return Response({"detail": "name and uri are required"}, status=400)
        try:
            dataset = register_dataset(name=name, uri=uri, description=description)
        except (ValueError, FileNotFoundError) as e:
            return Response({"detail": str(e)}, status=400)
        return Response(self.get_serializer(dataset).data, status=201)

    @action(detail=True, methods=["post"])
    def reregister(self, request, pk=None):
        ds = self.get_object()
        dataset = register_dataset(name=ds.name, uri=ds.uri, description=ds.description)
        return Response(self.get_serializer(dataset).data)


router = DefaultRouter()
router.register("datasets", DatasetViewSet)

urlpatterns = router.urls
