from django.urls import path
from rest_framework.routers import SimpleRouter

from .views import IngestStatusView, IngestWindowViewSet

router = SimpleRouter()
router.register("windows", IngestWindowViewSet, basename="ingest-window")

urlpatterns = [
    path("status/", IngestStatusView.as_view(), name="ingest-status"),
    *router.urls,
]
