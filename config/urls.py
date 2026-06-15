from django.contrib import admin
from django.urls import include, path

from apps.frontend import views as frontend_views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/catalog/", include("apps.catalog.api")),
    path("api/queries/", include("apps.queries.api")),
    path("", frontend_views.dashboard, name="dashboard"),
    path("datasets/<int:dataset_id>/", frontend_views.dataset_detail, name="dataset_detail"),
    path("queries/<int:run_id>/", frontend_views.query_run, name="query_run"),
    path("queries/<int:run_id>/status/", frontend_views.query_run_status, name="query_run_status"),
    path("run/", frontend_views.submit_query, name="submit_query"),
]
