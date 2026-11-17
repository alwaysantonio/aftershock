from datetime import UTC, datetime, timedelta

import pytest

from ingest import services, tasks
from ingest.client import PermanentError, TransientError, WindowTooLarge
from ingest.models import IngestWindow, RejectedRecord, SyncState, WindowStatus
from quakes.models import Earthquake

pytestmark = pytest.mark.django_db

DAY0 = datetime(2026, 1, 1, tzinfo=UTC)
HISTORY_START = datetime(2000, 1, 1, tzinfo=UTC)


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def feature(
    usgs_id: str, *, updated: datetime, mag: float = 4.6, time: datetime = DAY0
) -> dict:
    return {
        "id": usgs_id,
        "properties": {
            "mag": mag,
            "magType": "mb",
            "place": "Somewhere",
            "time": ms(time),
            "updated": ms(updated),
            "status": "reviewed",
            "type": "earthquake",
        },
        "geometry": {"coordinates": [142.3, 38.1, 10.0]},
    }


class FakeClient:
    """Answers each fetch from a queue of results; an Exception in the queue is raised."""

    def __init__(self, *results):
        self.results = list(results)
        self.queries = []

    def fetch(self, query):
        self.queries.append(query)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


def window(start=DAY0, days=1) -> IngestWindow:
    return IngestWindow.objects.create(start=start, end=start + timedelta(days=days))


def sync_state(watermark=DAY0) -> SyncState:
    return SyncState.objects.create(source=services.SYNC_SOURCE, watermark=watermark)


# ---------------------------------------------------------------- backfill


def test_window_success_stores_events_rejections_and_counts():
    w = window()
    bad = feature("bad", updated=DAY0)
    bad["geometry"]["coordinates"] = [0, 95, 0]  # impossible latitude
    client = FakeClient(
        [feature("us1", updated=DAY0), feature("us2", updated=DAY0), bad]
    )

    outcome = services.run_window(w.pk, client)

    w.refresh_from_db()
    assert outcome.status == "succeeded"
    assert (w.status, w.fetched, w.inserted, w.rejected, w.attempts) == (
        "succeeded",
        3,
        2,
        1,
        1,
    )
    assert Earthquake.objects.count() == 2
    assert RejectedRecord.objects.get().window == w


def test_finished_window_is_skipped_without_calling_usgs():
    w = window()
    services.run_window(w.pk, FakeClient([feature("us1", updated=DAY0)]))
    client = FakeClient()  # would fail if called

    outcome = services.run_window(w.pk, client)

    assert outcome.status == "skipped"
    assert client.queries == []


def test_oversized_window_is_split_into_two_halves():
    w = window()

    outcome = services.run_window(w.pk, FakeClient(WindowTooLarge(25_000)))

    w.refresh_from_db()
    halves = IngestWindow.objects.filter(pk__in=outcome.children).order_by("start")
    assert w.status == WindowStatus.SPLIT
    assert [(h.start, h.end) for h in halves] == [
        (DAY0, DAY0 + timedelta(hours=12)),
        (DAY0 + timedelta(hours=12), DAY0 + timedelta(days=1)),
    ]


def test_permanent_error_fails_the_window_without_retry():
    w = window()

    outcome = services.run_window(w.pk, FakeClient(PermanentError("400 bad request")))

    w.refresh_from_db()
    assert outcome.status == "failed"
    assert w.status == WindowStatus.FAILED and "400" in w.last_error


def test_transient_error_propagates_and_window_returns_to_pending():
    w = window()

    with pytest.raises(TransientError):
        services.run_window(w.pk, FakeClient(TransientError("503")))
    services.record_window_error(w.pk, TransientError("503"), final=False)

    w.refresh_from_db()
    assert w.status == WindowStatus.PENDING and w.attempts == 1


def test_create_windows_is_idempotent_and_returns_only_unfinished():
    first = services.create_windows(DAY0, DAY0 + timedelta(days=3))
    IngestWindow.objects.filter(pk=first[0]).update(status=WindowStatus.SUCCEEDED)

    second = services.create_windows(DAY0, DAY0 + timedelta(days=3))

    assert len(first) == 3 and IngestWindow.objects.count() == 3
    assert sorted(second) == sorted(first[1:])


# ---------------------------------------------------------------- sync


def test_sync_asks_for_changes_since_watermark_minus_overlap_including_deletions():
    sync_state(watermark=DAY0)
    client = FakeClient([])

    services.run_sync(
        client, history_start=HISTORY_START, now=DAY0 + timedelta(minutes=5)
    )

    query = client.queries[0]
    assert query.updatedafter == DAY0 - services.SYNC_OVERLAP
    assert query.starttime == HISTORY_START
    assert query.include_deleted is True


def test_sync_moves_watermark_to_newest_updated_saved():
    sync_state(watermark=DAY0)
    newest = DAY0 + timedelta(minutes=4)
    client = FakeClient(
        [
            feature("us1", updated=DAY0 + timedelta(minutes=1)),
            feature("us2", updated=newest),
        ]
    )

    outcome = services.run_sync(
        client, history_start=HISTORY_START, now=DAY0 + timedelta(minutes=5)
    )

    assert outcome.watermark == newest
    assert SyncState.objects.get().watermark == newest


def test_failed_sync_leaves_watermark_unchanged_and_records_error():
    sync_state(watermark=DAY0)

    with pytest.raises(TransientError):
        services.run_sync(
            FakeClient(TransientError("timeout")), history_start=HISTORY_START
        )

    state = SyncState.objects.get()
    assert state.watermark == DAY0
    assert "timeout" in state.last_error


def test_watermark_never_moves_backwards():
    sync_state(watermark=DAY0)
    late_old_version = feature(
        "us1", updated=DAY0 - timedelta(minutes=5)
    )  # inside the overlap

    services.run_sync(FakeClient([late_old_version]), history_start=HISTORY_START)

    assert SyncState.objects.get().watermark == DAY0


def test_sync_splits_event_time_range_when_too_many_changes():
    sync_state(watermark=DAY0)
    now = DAY0 + timedelta(days=1)
    client = FakeClient(
        WindowTooLarge(30_000),
        [feature("us1", updated=DAY0)],
        [feature("us2", updated=DAY0)],
    )

    outcome = services.run_sync(client, history_start=HISTORY_START, now=now)

    assert outcome.fetched == 2
    first_half, second_half = client.queries[1], client.queries[2]
    assert first_half.endtime == second_half.starttime  # halves meet exactly
    assert {q.updatedafter for q in client.queries} == {DAY0 - services.SYNC_OVERLAP}


# ---------------------------------------------------------------- bootstrap + tasks


def test_bootstrap_is_safe_to_run_on_every_start():
    now = DAY0 + timedelta(hours=10)

    first = services.bootstrap(backfill_days=3, if_empty=True, now=now)
    second = services.bootstrap(
        backfill_days=3, if_empty=True, now=now + timedelta(hours=1)
    )

    assert len(first) == 4  # 3 days back to midnight, plus today
    assert second == []
    state = SyncState.objects.get()
    assert (
        state.watermark == now
    )  # backfill start: first sync catches revisions made during it


def test_window_task_retries_transient_errors_then_marks_failed(monkeypatch):
    w = window()
    monkeypatch.setattr(
        tasks, "make_client", lambda: FakeClient(*[TransientError("503")] * 10)
    )
    monkeypatch.setattr(tasks, "retry_delay", lambda *a, **k: 0)

    result = tasks.ingest_window.apply(args=[w.pk])

    w.refresh_from_db()
    assert result.result == {"window": w.pk, "status": "failed"}
    assert w.status == WindowStatus.FAILED
    assert w.attempts == tasks.ingest_window.max_retries + 1  # first try + 5 retries
