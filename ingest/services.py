import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from quakes.models import Earthquake

from .client import (
    EventQuery,
    PermanentError,
    TransientError,
    USGSClient,
    WindowTooLarge,
)
from .models import IngestWindow, RecordOrigin, SyncState, WindowStatus
from .parsing import parse_features
from .upsert import UpsertResult, upsert_events
from .helpers import _midnight, _save_rejections, _set_status

log = logging.getLogger(__name__)

SYNC_SOURCE = "usgs"
SYNC_OVERLAP = timedelta(
    minutes=10
)  # re-fetch a little behind the watermark: covers late-indexed events
MIN_WINDOW = timedelta(minutes=1)  # stop splitting below this; something is badly wrong
DAY = timedelta(days=1)
DONE = (WindowStatus.SUCCEEDED, WindowStatus.SPLIT)


@dataclass
class WindowOutcome:
    status: str  # succeeded | skipped | split | failed
    children: list[int] = field(
        default_factory=list
    )  # windows to enqueue after a split
    result: UpsertResult | None = None


def create_windows(start: datetime, end: datetime, step: timedelta = DAY) -> list[int]:
    """
    Create one-day windows covering [start, end), aligned to midnight UTC.
    Idempotent: existing windows are left alone. Returns the ids that still
    need work (pending or previously failed), ready to enqueue.
    """
    start = _midnight(start)
    end = _midnight(end) + (DAY if end != _midnight(end) else timedelta())
    bounds = []
    cursor = start
    while cursor < end:
        bounds.append((cursor, min(cursor + step, end)))
        cursor += step

    IngestWindow.objects.bulk_create(
        [IngestWindow(start=s, end=e) for s, e in bounds],
        ignore_conflicts=True,  # unique (start, end): re-running creates nothing new
    )
    return list(
        IngestWindow.objects.filter(
            start__gte=start,
            end__lte=end,
            status__in=[WindowStatus.PENDING, WindowStatus.FAILED],
        ).values_list("id", flat=True)
    )


def run_window(window_id: int, client: USGSClient) -> WindowOutcome:
    """
    Fetch, parse and store one window. TransientError propagates so the
    caller (the Celery task) can retry later; everything else is handled here.
    """
    window = IngestWindow.objects.get(pk=window_id)
    if window.status in DONE:
        return WindowOutcome("skipped")

    IngestWindow.objects.filter(pk=window_id).update(
        status=WindowStatus.RUNNING,
        attempts=F("attempts") + 1,
        started_at=timezone.now(),
        last_error="",
    )

    # USGS's endtime is inclusive, so an event exactly on a boundary can be
    # fetched by two windows. The upsert makes that harmless.
    try:
        features = client.fetch(EventQuery(starttime=window.start, endtime=window.end))
    except WindowTooLarge as exc:
        return _split(window, exc)
    except PermanentError as exc:
        _set_status(window_id, WindowStatus.FAILED, error=str(exc))
        log.error("window %s failed permanently: %s", window_id, exc)
        return WindowOutcome("failed")

    events, rejections = parse_features(features)

    with transaction.atomic():
        # skip_locked: if another worker is writing this window right now, let it finish.
        locked = (
            IngestWindow.objects.select_for_update(skip_locked=True)
            .filter(pk=window_id)
            .first()
        )
        if locked is None or locked.status in DONE:
            return WindowOutcome("skipped")

        result = upsert_events(events)
        _save_rejections(rejections, RecordOrigin.BACKFILL, window=locked)

        locked.status = WindowStatus.SUCCEEDED
        locked.fetched = len(features)
        locked.inserted = result.inserted
        locked.updated_count = result.updated
        locked.rejected = len(rejections)
        locked.finished_at = timezone.now()
        locked.save()

    log.info(
        "window %s ok: fetched=%s inserted=%s updated=%s rejected=%s",
        window_id,
        len(features),
        result.inserted,
        result.updated,
        len(rejections),
    )
    return WindowOutcome("succeeded", result=result)


def record_window_error(window_id: int, exc: Exception, *, final: bool) -> None:
    """After a transient failure: back to pending for a retry, or failed if out of retries."""
    status = WindowStatus.FAILED if final else WindowStatus.PENDING
    _set_status(window_id, status, error=str(exc))


def _split(window: IngestWindow, exc: WindowTooLarge) -> WindowOutcome:
    if window.end - window.start <= MIN_WINDOW:
        _set_status(
            window.pk, WindowStatus.FAILED, error=f"cannot split further: {exc}"
        )
        return WindowOutcome("failed")

    mid = window.start + (window.end - window.start) / 2
    with transaction.atomic():
        IngestWindow.objects.bulk_create(
            [
                IngestWindow(start=window.start, end=mid),
                IngestWindow(start=mid, end=window.end),
            ],
            ignore_conflicts=True,
        )
        _set_status(window.pk, WindowStatus.SPLIT, error=str(exc))
        children = list(
            IngestWindow.objects.filter(
                start__gte=window.start,
                end__lte=window.end,
                status__in=[WindowStatus.PENDING, WindowStatus.FAILED],
            )
            .exclude(pk=window.pk)
            .values_list("id", flat=True)
        )
    log.info("window %s split into %s (%s)", window.pk, children, exc)
    return WindowOutcome("split", children=children)


@dataclass
class SyncOutcome:
    fetched: int
    result: UpsertResult
    watermark: datetime


def run_sync(
    client: USGSClient, *, history_start: datetime, now: datetime | None = None
) -> SyncOutcome:
    """
    Fetch everything USGS changed since the watermark (minus an overlap),
    including deletions, then store it and move the watermark forward.

    The HTTP fetch happens before any lock. If two syncs ever overlap, both
    writes are idempotent and the watermark only moves forward (max of the two),
    so the worst case is wasted work, never lost or duplicated data.
    """
    now = now or timezone.now()
    state = SyncState.objects.get(source=SYNC_SOURCE)
    since = state.watermark - SYNC_OVERLAP

    try:
        features = _fetch_changes(client, history_start, now, since)
    except (TransientError, PermanentError) as exc:
        SyncState.objects.filter(pk=state.pk).update(
            last_run_at=now, last_error=str(exc)[:2000]
        )
        raise

    events, rejections = parse_features(features)

    with transaction.atomic():
        state = SyncState.objects.select_for_update().get(pk=state.pk)
        result = upsert_events(events)
        _save_rejections(rejections, RecordOrigin.SYNC)

        # Only after the data is written, in the same transaction. Never backwards.
        if result.newest_updated and result.newest_updated > state.watermark:
            state.watermark = result.newest_updated
        state.last_run_at = now
        state.last_success_at = now
        state.last_error = ""
        state.save()

    log.info(
        "sync ok: since=%s fetched=%s inserted=%s updated=%s watermark=%s",
        since.isoformat(),
        len(features),
        result.inserted,
        result.updated,
        state.watermark.isoformat(),
    )
    return SyncOutcome(fetched=len(features), result=result, watermark=state.watermark)


def _fetch_changes(
    client: USGSClient, start: datetime, end: datetime, since: datetime
) -> list[dict]:
    """
    One query normally. After a long outage more than 20,000 events may have
    changed, so split the *event-time* range in half until each part fits,
    keeping the same updatedafter.
    """
    query = EventQuery(
        starttime=start, endtime=end, updatedafter=since, include_deleted=True
    )
    try:
        return client.fetch(query)
    except WindowTooLarge:
        if end - start <= MIN_WINDOW:
            raise PermanentError("too many changes even in a one-minute range")
        mid = start + (end - start) / 2
        return _fetch_changes(client, start, mid, since) + _fetch_changes(
            client, mid, end, since
        )


def bootstrap(
    *, backfill_days: int, if_empty: bool, now: datetime | None = None
) -> list[int]:
    """
    First-start setup. Creates the sync bookmark and, optionally, the windows
    for an initial backfill. Safe to run on every start.

    The watermark starts at "now", i.e. when the backfill begins, so anything
    USGS revises while the backfill runs is picked up by the first sync.
    """
    now = now or timezone.now()
    SyncState.objects.get_or_create(source=SYNC_SOURCE, defaults={"watermark": now})

    if backfill_days <= 0:
        return []
    if if_empty and (Earthquake.objects.exists() or IngestWindow.objects.exists()):
        return []
    return create_windows(now - timedelta(days=backfill_days), now)
