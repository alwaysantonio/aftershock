from enum import StrEnum
from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Now


class WindowStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SPLIT = "split"  # too many events for one query; replaced by two half windows


class IngestWindow(models.Model):
    """
    Windows are independent: one can fail and be retried while others succeed.
    The unique (start, end) constraint makes the backfill command idempotent:
    running it twice creates no duplicate windows.
    """

    start = models.DateTimeField()
    end = models.DateTimeField()

    status = models.CharField(
        max_length=16,
        choices=[(member.value, member.value) for member in WindowStatus],
        default=WindowStatus.PENDING.value,
    )
    attempts = models.PositiveIntegerField(default=0)

    fetched = models.PositiveIntegerField(default=0)
    inserted = models.PositiveIntegerField(default=0)
    updated_count = models.PositiveIntegerField(
        default=0
    )  # rows replaced by a newer version
    rejected = models.PositiveIntegerField(default=0)

    last_error = models.TextField(blank=True)

    created_at = models.DateTimeField(db_default=Now())
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["start"]
        constraints = [
            models.UniqueConstraint(
                fields=["start", "end"], name="window_unique_range"
            ),
            models.CheckConstraint(
                condition=Q(end__gt=F("start")), name="window_end_after_start"
            ),
        ]
        indexes = [
            # "Which windows still need work?"
            models.Index(
                fields=["status", "start"],
                condition=~Q(status=WindowStatus.SUCCEEDED.value),
                name="window_unfinished_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.start:%Y-%m-%d} → {self.end:%Y-%m-%d} [{self.status}]"


class SyncState(models.Model):
    """
    The ONGOING sync's bookmark. One row per data source.

    `watermark` = the highest USGS `updated` value we have successfully saved.
    Each sync asks USGS for `updatedafter = watermark - overlap`, and moves the
    watermark forward only after its batch commits. A failed run leaves it
    unchanged, so the next run re-fetches rather than skipping anything.
    """

    source = models.CharField(max_length=50, unique=True)  # "usgs"
    watermark = models.DateTimeField()

    last_run_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    def __str__(self) -> str:
        return f"{self.source} @ {self.watermark:%Y-%m-%d %H:%M:%S}"


class RecordOrigin(StrEnum):
    BACKFILL = "backfill"
    SYNC = "sync"


class RejectedRecord(models.Model):
    """
    Quarantine for features that failed validation. The batch carries on
    without them; this keeps the evidence and the reason.
    """

    origin = models.CharField(
        max_length=16, choices=[(member.value, member.value) for member in RecordOrigin]
    )
    window = models.ForeignKey(
        IngestWindow,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rejected_records",
    )

    usgs_id = models.CharField(max_length=32, blank=True)
    reason = models.CharField(max_length=255)
    payload = models.JSONField()

    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["-created_at"], name="rejected_created_idx"),
            models.Index(fields=["usgs_id"], name="rejected_usgs_id_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.usgs_id or '<no id>'}: {self.reason}"
