from datetime import UTC, datetime, timedelta

import pytest

from ingest.parsing import ParsedEvent
from ingest.upsert import upsert_events
from quakes.models import Earthquake, EarthquakeRevision

pytestmark = pytest.mark.django_db

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def event(
    usgs_id="us1", *, updated=T0, mag=4.6, status="reviewed", **kw
) -> ParsedEvent:
    fields = dict(
        usgs_id=usgs_id,
        time=T0 - timedelta(hours=1),
        updated=updated,
        status=status,
        is_deleted=status == "deleted",
        mag=mag,
        mag_type="mb",
        latitude=38.1,
        longitude=142.3,
        depth_km=10.0,
        place="Somewhere",
        raw={"id": usgs_id, "mag": mag},
    )
    fields.update(kw)
    return ParsedEvent(**fields)


def stripped_deletion(usgs_id="us1", *, updated) -> ParsedEvent:
    return ParsedEvent(
        usgs_id=usgs_id,
        time=None,
        updated=updated,
        status="deleted",
        is_deleted=True,
        raw={"id": usgs_id, "status": "deleted"},
    )


def test_new_events_are_inserted_with_a_revision_each():
    result = upsert_events([event("us1"), event("us2")])

    assert (result.inserted, result.updated, result.unchanged) == (2, 0, 0)
    assert Earthquake.objects.count() == 2
    assert EarthquakeRevision.objects.count() == 2
    assert result.newest_updated == T0


def test_running_the_same_batch_twice_changes_nothing():
    batch = [event("us1"), event("us2")]
    upsert_events(batch)

    second = upsert_events(batch)

    assert (second.inserted, second.updated, second.unchanged) == (0, 0, 2)
    assert Earthquake.objects.count() == 2
    assert EarthquakeRevision.objects.count() == 2  # history not duplicated


def test_newer_version_overwrites_and_is_added_to_history():
    upsert_events([event(mag=4.6, updated=T0)])

    result = upsert_events([event(mag=4.9, updated=T0 + timedelta(minutes=5))])

    assert result.updated == 1
    assert Earthquake.objects.get().mag == 4.9
    assert list(
        EarthquakeRevision.objects.order_by("updated").values_list("mag", flat=True)
    ) == [4.6, 4.9]


def test_older_version_never_overwrites_newer_but_is_kept_in_history():
    upsert_events([event(mag=4.9, updated=T0 + timedelta(minutes=5))])

    result = upsert_events([event(mag=4.6, updated=T0)])  # arrives late

    assert (result.updated, result.unchanged) == (0, 1)
    assert Earthquake.objects.get().mag == 4.9
    assert EarthquakeRevision.objects.count() == 2


def test_stripped_deletion_hides_an_existing_event():
    upsert_events([event()])

    result = upsert_events([stripped_deletion(updated=T0 + timedelta(hours=1))])

    quake = Earthquake.objects.get()
    assert result.updated == 1
    assert quake.is_deleted and quake.status == "deleted"
    assert quake.latitude == 38.1  # location kept from the last full version
    assert not Earthquake.objects.visible().exists()


def test_stripped_deletion_for_unknown_event_is_skipped_not_inserted():
    result = upsert_events([stripped_deletion("never-seen", updated=T0)])

    assert result.skipped == 1
    assert not Earthquake.objects.exists()


def test_batches_commit_independently_and_totals_add_up():
    events = [event(f"us{i}") for i in range(5)]

    result = upsert_events(events, batch_size=2)  # 3 batches: 2 + 2 + 1

    assert result.inserted == 5
    assert Earthquake.objects.count() == 5
