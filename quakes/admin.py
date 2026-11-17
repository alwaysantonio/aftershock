import json
from functools import cached_property

from django.contrib import admin
from django.core.paginator import Paginator
from django.db import connection
from django.utils.html import format_html

from .models import Earthquake, EarthquakeRevision

admin.site.site_header = "aftershock admin"
admin.site.site_title = "aftershock"


EXACT_COUNT_BELOW = 10_000


class EstimatedCountPaginator(Paginator):
    @cached_property
    def count(self):
        sql, params = self.object_list.query.sql_with_params()
        with connection.cursor() as cursor:
            cursor.execute(f"EXPLAIN (FORMAT JSON) {sql}", params)
            estimate = int(cursor.fetchone()[0][0]["Plan"]["Plan Rows"])
        return self.object_list.count() if estimate < EXACT_COUNT_BELOW else estimate


class VisibilityFilter(admin.SimpleListFilter):
    """Visible events by default (fast: the API's partial index); deleted ones on request."""

    title = "visibility"
    parameter_name = "visibility"

    def lookups(self, request, model_admin):
        return [("visible", "Visible"), ("deleted", "Deleted by USGS")]

    def choices(self, changelist):
        # No "All" option: the default is "visible", and "all" would sort the whole table.
        for value, label in self.lookup_choices:
            yield {
                "selected": (self.value() or "visible") == value,
                "query_string": changelist.get_query_string(
                    {self.parameter_name: value}
                ),
                "display": label,
            }

    def queryset(self, request, queryset):
        return queryset.filter(is_deleted=self.value() == "deleted")


class ReadOnlyAdminMixin:
    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class RevisionInline(ReadOnlyAdminMixin, admin.TabularInline):
    model = EarthquakeRevision
    fields = [
        "updated",
        "received_at",
        "mag",
        "mag_type",
        "status",
        "latitude",
        "longitude",
        "depth_km",
    ]
    readonly_fields = fields
    ordering = ["-updated"]
    extra = 0
    show_change_link = False

    def get_queryset(self, request):
        return super().get_queryset(request).defer("raw")


@admin.register(Earthquake)
class EarthquakeAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = [
        "usgs_id",
        "time",
        "mag",
        "mag_type",
        "place",
        "status",
        "is_deleted",
        "updated",
    ]
    # No event_type filter: its sidebar runs SELECT DISTINCT over the whole table on every page.
    list_filter = [VisibilityFilter, "status", "tsunami"]
    search_fields = ["usgs_id"]  # see get_search_results: exact match only
    search_help_text = "Exact USGS id, e.g. us7000n5no"
    ordering = ["-time", "-id"]
    list_per_page = 50
    paginator = EstimatedCountPaginator
    show_full_result_count = False  # skips a second COUNT(*) of the unfiltered table
    inlines = [RevisionInline]
    readonly_fields = ["usgs_link", "pretty_raw"]
    fieldsets = [
        (
            None,
            {
                "fields": [
                    "usgs_id",
                    "usgs_link",
                    "time",
                    "updated",
                    "status",
                    "is_deleted",
                ]
            },
        ),
        (
            "Measurement",
            {
                "fields": [
                    "mag",
                    "mag_type",
                    "latitude",
                    "longitude",
                    "depth_km",
                    "sig",
                    "tsunami",
                ]
            },
        ),
        ("Description", {"fields": ["place", "event_type"]}),
        ("Ingestion", {"fields": ["created_at", "last_ingested_at"]}),
        ("Source payload", {"fields": ["pretty_raw"], "classes": ["collapse"]}),
    ]

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        # The list page never shows `raw`; only the detail page needs it.
        if request.resolver_match and request.resolver_match.url_name.endswith(
            "_changelist"
        ):
            queryset = queryset.defer("raw")
        return queryset

    def get_search_results(self, request, queryset, search_term):
        # Default admin search is icontains (LIKE '%…%'): a full scan of millions
        # of rows. An exact match uses the unique index instead.
        term = search_term.strip()
        if not term:
            return queryset, False
        return queryset.filter(usgs_id=term), False

    @admin.display(description="USGS page")
    def usgs_link(self, obj):
        url = f"https://earthquake.usgs.gov/earthquakes/eventpage/{obj.usgs_id}"
        return format_html(
            '<a href="{}" target="_blank" rel="noopener">{}</a>', url, url
        )

    @admin.display(description="Raw feature")
    def pretty_raw(self, obj):
        return format_html(
            "<pre style='white-space: pre-wrap'>{}</pre>",
            json.dumps(obj.raw, indent=2, sort_keys=True),
        )
