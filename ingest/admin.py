import json

from django.contrib import admin, messages
from django.db import transaction
from django.utils import timezone
from django.utils.html import format_html

from quakes.admin import ReadOnlyAdminMixin

from .models import IngestWindow, RejectedRecord, SyncState, WindowStatus
from .tasks import enqueue_windows

RETRYABLE = [WindowStatus.FAILED, WindowStatus.PENDING]


@admin.register(IngestWindow)
class IngestWindowAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = [
        "start",
        "end",
        "status",
        "attempts",
        "fetched",
        "inserted",
        "updated_count",
        "rejected",
        "finished_at",
        "short_error",
    ]
    list_filter = ["status"]
    ordering = ["-start"]
    list_per_page = 100
    actions = ["retry_windows"]

    @admin.display(description="Last error")
    def short_error(self, obj):
        return (
            (obj.last_error[:80] + "…") if len(obj.last_error) > 80 else obj.last_error
        )

    @admin.action(description="Retry selected windows (failed or pending only)")
    def retry_windows(self, request, queryset):
        with transaction.atomic():
            ids = list(
                queryset.filter(status__in=RETRYABLE).values_list("id", flat=True)
            )
            IngestWindow.objects.filter(id__in=ids).update(
                status=WindowStatus.PENDING, last_error=""
            )
            enqueue_windows(ids)  # sent once this transaction commits
        skipped = queryset.count() - len(ids)
        self.message_user(
            request, f"Queued {len(ids)} window(s) for retry.", messages.SUCCESS
        )
        if skipped:
            self.message_user(
                request,
                f"Skipped {skipped} already running, succeeded or split.",
                messages.WARNING,
            )

    def has_change_permission(self, request, obj=None):
        # Actions need change permission on the list; individual rows stay read-only.
        return obj is None and request.user.is_staff


@admin.register(SyncState)
class SyncStateAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ["source", "watermark", "last_success_at", "lag", "last_error"]

    @admin.display(description="Time since last success")
    def lag(self, obj):
        if obj.last_success_at is None:
            return "never"
        seconds = int((timezone.now() - obj.last_success_at).total_seconds())
        return f"{seconds // 60} min {seconds % 60} s"


@admin.register(RejectedRecord)
class RejectedRecordAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ["created_at", "origin", "usgs_id", "reason", "window"]
    list_filter = ["origin"]
    search_fields = ["usgs_id"]
    ordering = ["-created_at"]
    list_select_related = [
        "window"
    ]  # one JOIN, not a query per row for the window column
    readonly_fields = ["pretty_payload"]
    fields = ["created_at", "origin", "usgs_id", "reason", "window", "pretty_payload"]

    @admin.display(description="Payload")
    def pretty_payload(self, obj):
        return format_html(
            "<pre style='white-space: pre-wrap'>{}</pre>",
            json.dumps(obj.payload, indent=2, sort_keys=True),
        )

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser  # clearing reviewed rejections is allowed
