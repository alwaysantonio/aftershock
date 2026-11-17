from django.contrib import admin
from django.urls import include, path

from .health import healthz

urlpatterns = [
    path("healthz", healthz, name="healthz"),
    path("api/", include("quakes.urls")),
    path("api/ingest/", include("ingest.urls")),
    path("admin/", admin.site.urls),
    path("api-auth/", include("rest_framework.urls")),
]
