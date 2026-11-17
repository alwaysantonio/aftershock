"""Parser tests: pure functions, no database."""

import copy
from datetime import UTC, datetime

import pytest

from ingest.parsing import ParsedEvent, Rejection, parse_feature, parse_features

TIME_MS = 1_767_225_600_000  # 2026-01-01T00:00:00Z
UPDATED_MS = 1_767_229_200_000  # 2026-01-01T01:00:00Z


def feature(**overrides):
    """A realistic USGS feature; override properties or geometry per test."""
    f = {
        "type": "Feature",
        "id": "us7000test",
        "properties": {
            "mag": 4.6,
            "magType": "mb",
            "place": "45 km SW of Somewhere",
            "time": TIME_MS,
            "updated": UPDATED_MS,
            "status": "reviewed",
            "type": "earthquake",
            "tsunami": 0,
            "sig": 326,
        },
        "geometry": {"type": "Point", "coordinates": [142.3, 38.1, 10.0]},
    }
    geometry = overrides.pop("geometry", None)
    if geometry is not None:
        f["geometry"] = geometry
    f["properties"].update(overrides)
    return f


def test_valid_feature_becomes_clean_event():
    event = parse_feature(feature())

    assert isinstance(event, ParsedEvent)
    assert event.usgs_id == "us7000test"
    assert event.time == datetime(2026, 1, 1, tzinfo=UTC)
    assert event.updated == datetime(2026, 1, 1, 1, tzinfo=UTC)
    assert (event.longitude, event.latitude, event.depth_km) == (142.3, 38.1, 10.0)
    assert event.mag == 4.6 and event.mag_type == "mb"
    assert event.is_deleted is False and event.is_complete


def test_missing_optional_values_are_kept_empty_not_rejected():
    event = parse_feature(feature(mag=None, sig=None, place=None, magType=None))

    assert isinstance(event, ParsedEvent)
    assert event.mag is None and event.sig is None
    assert event.place == "" and event.mag_type == ""


@pytest.mark.parametrize(
    ("overrides", "reason_fragment"),
    [
        ({"geometry": {"coordinates": [10.0, 91.2, 5.0]}}, "latitude out of range"),
        ({"geometry": {"coordinates": [181.0, 10.0, 5.0]}}, "longitude out of range"),
        ({"mag": 14.0}, "mag out of range"),
        ({"mag": float("nan")}, "mag is not finite"),
        ({"mag": True}, "mag is not a number"),  # bool would otherwise become 1.0
        ({"mag": "4.6"}, "mag is not a number"),
        ({"time": None}, "missing time"),
        ({"updated": "yesterday"}, "updated is not an epoch"),
        ({"status": "unknown"}, "unknown status"),
        ({"geometry": {"coordinates": [10.0]}}, "coordinates must be"),
    ],
)
def test_impossible_values_are_rejected_with_a_reason(overrides, reason_fragment):
    result = parse_feature(feature(**overrides))

    assert isinstance(result, Rejection)
    assert result.usgs_id == "us7000test"
    assert reason_fragment in result.reason


def test_missing_id_is_rejected_and_payload_kept():
    bad = feature()
    del bad["id"]

    result = parse_feature(bad)

    assert isinstance(result, Rejection)
    assert result.usgs_id == "" and result.payload is bad


def test_stripped_deletion_is_accepted_but_cannot_be_inserted():
    deletion = {
        "id": "us7000test",
        "properties": {"status": "deleted", "updated": UPDATED_MS},
        "geometry": None,
    }

    event = parse_feature(deletion)

    assert isinstance(event, ParsedEvent)
    assert event.is_deleted is True
    assert event.is_complete is False  # the upsert may only update an existing row


def test_long_text_is_truncated_not_rejected():
    event = parse_feature(feature(place="x" * 400))

    assert isinstance(event, ParsedEvent)
    assert len(event.place) == 255


def test_batch_keeps_only_the_newest_version_of_each_event():
    older = feature()
    newer = copy.deepcopy(older)
    newer["properties"].update(updated=UPDATED_MS + 60_000, mag=4.9)

    events, rejections = parse_features([newer, older, feature(mag=99)])

    assert [e.mag for e in events] == [4.9]
    assert len(rejections) == 1
