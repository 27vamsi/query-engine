from django.contrib import admin
from django.urls import include, path

from apps.frontend import views as frontend_views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/catalog/", include("apps.catalog.api")),
    path("api/queries/", include("apps.queries.api")),
    # `dashboard` now renders the Overview page. The route name is unchanged so
    # existing {% url 'dashboard' %} references and bookmarks keep resolving.
    path("", frontend_views.dashboard, name="dashboard"),
    path("workspace/", frontend_views.workspace, name="workspace"),
    path("workspace/<int:dataset_id>/", frontend_views.workspace, name="workspace_dataset"),
    path("workspace/explain/", frontend_views.explain_query, name="explain_query"),
    path("datasets/", frontend_views.datasets, name="datasets"),
    path("datasets/<int:dataset_id>/", frontend_views.dataset_detail, name="dataset_detail"),
    path("history/", frontend_views.history, name="history"),
    path("plans/", frontend_views.plans, name="plans"),
    path("performance/", frontend_views.performance, name="performance"),
    path("capabilities/", frontend_views.capabilities, name="capabilities"),
    path("queries/<int:run_id>/", frontend_views.query_run, name="query_run"),
    path("queries/<int:run_id>/status/", frontend_views.query_run_status, name="query_run_status"),
    path("queries/<int:run_id>/rerun/", frontend_views.rerun_query, name="rerun_query"),
    path("run/", frontend_views.submit_query, name="submit_query"),
]
