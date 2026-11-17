from datetime import UTC, date, datetime

from django.db import transaction
from django.db.models import Count
from rest_framework import mixins, status, viewsets
from rest_framework.pagination import CursorPagination
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services
from .models import IngestWindow, RejectedRecord, SyncState
from .tasks import enqueue_windows
from .serializers import BackfillRequestSerializer, IngestWindowSerializer


class WindowPagination(CursorPagination):
    ordering = ("-start", "-id")
    page_size = 100


class IngestWindowViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    """
    GET  /api/ingest/windows/?status=failed   backfill progress
    POST /api/ingest/windows/  {"start": "2025-01-01", "end": "2025-02-01"}   queue a backfill (202)
    """

    permission_classes = [IsAdminUser]
    queryset = IngestWindow.objects.all()
    serializer_class = IngestWindowSerializer
    pagination_class = WindowPagination
    filterset_fields = ["status"]

    def create(self, request):
        body = BackfillRequestSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        start, end = (
            datetime.combine(body.validated_data[k], datetime.min.time(), tzinfo=UTC)
            for k in ("start", "end")
        )

        with transaction.atomic():
            services.bootstrap(
                backfill_days=0, if_empty=False
            )  # make sure the sync bookmark exists
            ids = services.create_windows(start, end)
            enqueue_windows(ids)  # sent once this transaction commits

        # 202: accepted for processing; progress is visible at GET /api/ingest/windows/
        return Response(
            {"queued": len(ids), "start": start.date(), "end": end.date()},
            status=status.HTTP_202_ACCEPTED,
        )


class IngestStatusView(APIView):
    """GET /api/ingest/status/: one-glance health of both ingestion jobs."""

    permission_classes = [IsAdminUser]

    def get(self, request):
        state = SyncState.objects.filter(source=services.SYNC_SOURCE).first()
        windows = dict(
            IngestWindow.objects.values_list("status")
            .annotate(n=Count("id"))
            .values_list("status", "n")
        )
        return Response(
            {
                "sync": (
                    None
                    if state is None
                    else {
                        "watermark": state.watermark,
                        "last_run_at": state.last_run_at,
                        "last_success_at": state.last_success_at,
                        "last_error": state.last_error,
                    }
                ),
                "windows": windows,
                "rejected_records": RejectedRecord.objects.count(),
            }
        )
