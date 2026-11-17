from enum import StrEnum

from django.db import models
from django.db.models import Q
from django.db.models.functions import Now


class EventStatus(StrEnum):
    AUTOMATIC = "automatic"
    REVIEWED = "reviewed"
    DELETED = "deleted"


class EarthquakeQuerySet(models.QuerySet):
    def visible(self):
        """What the API serves: everything USGS hasn't withdrawn."""
        return self.filter(is_deleted=False)


class Earthquake(models.Model):
    """
    One row per USGS event, always holding its LATEST known version.

    Written by a raw-SQL conditional upsert (ON CONFLICT ... WHERE updated is
    newer), so timestamps use db_default rather than auto_now: auto_now only
    fires through the ORM's save() and would be skipped by raw SQL.
    """

    usgs_id = models.CharField(max_length=32, unique=True)

    time = models.DateTimeField()
    updated = models.DateTimeField()

    mag = models.FloatField(null=True, blank=True)
    mag_type = models.CharField(max_length=16, blank=True)

    latitude = models.FloatField()
    longitude = models.FloatField()
    # USGS reports km, positive down; very shallow events can be slightly negative.
    depth_km = models.FloatField(null=True, blank=True)

    place = models.CharField(max_length=255, blank=True)
    status = models.CharField(
        max_length=16,
        choices=[(member.value, member.value) for member in EventStatus],
        default=EventStatus.AUTOMATIC.value,
    )
    event_type = models.CharField(max_length=32, default="earthquake")
    tsunami = models.BooleanField(default=False)
    sig = models.IntegerField(null=True, blank=True)  # USGS significance score

    # Soft delete: USGS withdraws events; we hide them but keep the record.
    is_deleted = models.BooleanField(default=False)

    raw = models.JSONField()

    created_at = models.DateTimeField(db_default=Now())
    last_ingested_at = models.DateTimeField(db_default=Now())

    objects = EarthquakeQuerySet.as_manager()

    class Meta:
        ordering = ["-time", "-id"]
        indexes = [
            # Default sort + keyset (cursor) pagination. Partial: the API only
            # ever reads non-deleted rows, so the index skips the rest.
            models.Index(
                fields=["-time", "-id"],
                condition=Q(is_deleted=False),
                name="quake_visible_time_id_idx",
            ),
            # "Big quakes in a date range": WHERE mag >= x AND time BETWEEN ...
            models.Index(
                fields=["mag", "time"],
                condition=Q(is_deleted=False),
                name="quake_visible_mag_time_idx",
            ),
            # Bounding-box filters. PostGIS + a GiST index is the production upgrade.
            models.Index(fields=["latitude", "longitude"], name="quake_lat_lon_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(latitude__gte=-90) & Q(latitude__lte=90),
                name="quake_latitude_range",
            ),
            models.CheckConstraint(
                condition=Q(longitude__gte=-180) & Q(longitude__lte=180),
                name="quake_longitude_range",
            ),
            models.CheckConstraint(
                condition=Q(mag__isnull=True) | (Q(mag__gte=-2) & Q(mag__lte=10)),
                name="quake_mag_range",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.usgs_id} M{self.mag} {self.place}"


class EarthquakeRevision(models.Model):
    """
    Append-only history: one row per distinct version of an event we've seen.
    """

    earthquake = models.ForeignKey(
        Earthquake, on_delete=models.CASCADE, related_name="revisions"
    )
    updated = models.DateTimeField()

    # Snapshot of the fields worth comparing across versions.
    mag = models.FloatField(null=True, blank=True)
    mag_type = models.CharField(max_length=16, blank=True)
    status = models.CharField(
        max_length=16,
        choices=[(member.value, member.value) for member in EventStatus],
        default=EventStatus.AUTOMATIC.value,
    )
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    depth_km = models.FloatField(null=True, blank=True)

    raw = models.JSONField()
    received_at = models.DateTimeField(db_default=Now())

    class Meta:
        ordering = ["-updated"]
        constraints = [
            # Also serves as the index for "this quake's history, newest first".
            models.UniqueConstraint(
                fields=["earthquake", "updated"], name="revision_unique_version"
            ),
        ]
        indexes = [
            # "Recently revised" feed.
            models.Index(fields=["-received_at"], name="revision_received_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.earthquake_id} @ {self.updated:%Y-%m-%d %H:%M:%S}"
