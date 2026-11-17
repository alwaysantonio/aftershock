from django.db.models import Prefetch
from django.shortcuts import get_object_or_404
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from config.exceptions import Gone

from .filters import EarthquakeFilter
from .models import Earthquake, EarthquakeRevision
from .pagination import EarthquakeCursorPagination, RevisionCursorPagination
from .serializers import (
    EarthquakeDetailSerializer,
    EarthquakeSerializer,
    RevisionSerializer,
)

EXPANDED_REVISIONS = 50  # newest N embedded on detail; the full history is /revisions/


def expand(request) -> set[str]:
    return {
        part.strip()
        for part in request.query_params.get("expand", "").split(",")
        if part.strip()
    }


class EarthquakeViewSet(viewsets.ReadOnlyModelViewSet):
    """
    list:      GET /api/earthquakes/?start=&end=&min_mag=&max_mag=&bbox=&status=&type=&updated_since=
    retrieve:  GET /api/earthquakes/{usgs_id}/?expand=revisions   (410 if USGS deleted it)
    revisions: GET /api/earthquakes/{usgs_id}/revisions/
    """

    lookup_field = "usgs_id"
    lookup_value_regex = r"[A-Za-z0-9_\-]+"
    filterset_class = EarthquakeFilter
    pagination_class = EarthquakeCursorPagination

    def get_queryset(self):
        # `raw` (the full source JSON) is the biggest column and the API never shows it.
        queryset = Earthquake.objects.defer("raw")
        return queryset.visible() if self.action == "list" else queryset

    def get_serializer_class(self):
        if self.action == "retrieve" and "revisions" in expand(self.request):
            return EarthquakeDetailSerializer
        return EarthquakeSerializer

    def get_object(self):
        # No list filters on detail lookups: they'd make ?expand= or a stray
        # parameter change which object is found.
        return get_object_or_404(self.get_queryset(), usgs_id=self.kwargs["usgs_id"])

    def retrieve(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        if "revisions" in expand(request):
            recent = EarthquakeRevision.objects.defer("raw").order_by("-updated")[
                :EXPANDED_REVISIONS
            ]
            queryset = queryset.prefetch_related(
                Prefetch("revisions", queryset=recent, to_attr="recent_revisions")
            )
        quake = get_object_or_404(queryset, usgs_id=kwargs["usgs_id"])
        if quake.is_deleted:
            raise Gone(f"Earthquake {quake.usgs_id} was withdrawn by USGS.")
        return Response(self.get_serializer(quake).data)

    @action(detail=True, methods=["get"])
    def revisions(self, request, usgs_id=None):
        # History stays readable for deleted events: it shows when they were withdrawn.
        quake = self.get_object()
        paginator = RevisionCursorPagination()
        page = paginator.paginate_queryset(
            EarthquakeRevision.objects.filter(earthquake=quake).defer("raw"),
            request,
            view=self,
        )
        return paginator.get_paginated_response(
            RevisionSerializer(page, many=True).data
        )
