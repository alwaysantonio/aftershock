"""Public API tests: real Postgres, Django test client via DRF's APIClient."""

from datetime import UTC, datetime, timedelta

import pytest
from rest_framework.test import APIClient
from rest_framework.throttling import AnonRateThrottle

from quakes.models import Earthquake, EarthquakeRevision

pytestmark = pytest.mark.django_db

T0 = datetime(2026, 1, 1, tzinfo=UTC)
LIST = "/api/earthquakes/"


@pytest.fixture
def api():
    return APIClient()


def quake(usgs_id, *, hours=0, mag=4.5, lat=38.0, lon=142.0, **kw) -> Earthquake:
    defaults = dict(
        time=T0 + timedelta(hours=hours),
        updated=T0 + timedelta(hours=hours),
        mag=mag,
        mag_type="mb",
        latitude=lat,
        longitude=lon,
        depth_km=10.0,
        place="Somewhere",
        status="reviewed",
        raw={"id": usgs_id},
    )
    defaults.update(kw)
    return Earthquake.objects.create(usgs_id=usgs_id, **defaults)


def ids(response) -> list[str]:
    return [row["id"] for row in response.json()["results"]]


def error(response) -> dict:
    body = response.json()
    assert set(body) == {"error"} and set(body["error"]) == {
        "code",
        "message",
        "details",
    }
    return body["error"]


#  list


def test_list_is_newest_first_hides_deleted_and_never_exposes_internals(api):
    quake("old", hours=1)
    quake("new", hours=2)
    quake("gone", hours=3, is_deleted=True, status="deleted")

    response = api.get(LIST)

    assert response.status_code == 200
    assert ids(response) == ["new", "old"]
    row = response.json()["results"][0]
    assert "raw" not in row and "is_deleted" not in row
    assert row["usgs_url"].endswith("/eventpage/new")


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"min_mag": 5}, ["big"]),
        ({"max_mag": 4}, ["small"]),
        ({"start": "2026-01-01T01:30:00Z"}, ["big"]),
        ({"end": "2026-01-01T01:30:00Z"}, ["small"]),
        ({"bbox": "0,0,10,10"}, ["small"]),
        ({"type": "Quarry Blast"}, ["small"]),
        ({"status": "automatic"}, ["big"]),
    ],
)
def test_filters(api, params, expected):
    quake("small", hours=1, mag=3.0, lat=5, lon=5, event_type="quarry blast")
    quake("big", hours=2, mag=6.1, lat=38, lon=142, status="automatic")

    assert ids(api.get(LIST, params)) == expected


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"min_mag": 6, "max_mag": 5}, "max_mag"),
        ({"start": "2026-02-01", "end": "2026-01-01"}, "end"),
        ({"bbox": "1,2,3"}, "bbox"),
        ({"bbox": "0,0,10,95"}, "bbox"),
        ({"min_mag": "lots"}, "min_mag"),
        ({"start": "yesterday"}, "start"),
        ({"status": "deleted"}, "status"),  # deleted events aren't listable
    ],
)
def test_invalid_filters_return_400_in_the_standard_error_shape(api, params, field):
    response = api.get(LIST, params)

    assert response.status_code == 400
    err = error(response)
    assert err["code"] == "invalid" and field in err["details"]


def test_cursor_pagination_never_repeats_or_skips_when_new_rows_arrive(api):
    for i in range(5):
        quake(f"q{i}", hours=i)

    page1 = api.get(LIST, {"page_size": 2}).json()
    quake("brand-new", hours=99)  # arrives between page requests
    page2 = api.get(page1["next"]).json()
    page3 = api.get(page2["next"]).json()

    seen = [r["id"] for p in (page1, page2, page3) for r in p["results"]]
    assert seen == ["q4", "q3", "q2", "q1", "q0"]  # no duplicates, nothing skipped
    assert page3["next"] is None


def test_list_query_count_does_not_grow_with_rows(api, django_assert_max_num_queries):
    for i in range(40):
        quake(f"q{i}", hours=i)

    with django_assert_max_num_queries(
        1
    ):  # one SELECT for the page, no N+1, no COUNT(*)
        response = api.get(LIST, {"page_size": 40})

    assert len(response.json()["results"]) == 40


# detail + history


def test_detail_by_usgs_id(api):
    quake("us1")

    response = api.get("/api/earthquakes/us1/")

    assert response.status_code == 200 and response.json()["id"] == "us1"
    assert "revisions" not in response.json()


def test_unknown_event_is_404_in_the_standard_shape(api):
    response = api.get("/api/earthquakes/nope/")

    assert response.status_code == 404
    assert error(response)["code"] == "not_found"


def test_deleted_event_is_410_gone_but_history_stays_readable(api):
    q = quake("us1", is_deleted=True, status="deleted")
    EarthquakeRevision.objects.create(
        earthquake=q, updated=T0, status="deleted", raw={}
    )

    detail = api.get("/api/earthquakes/us1/")
    history = api.get("/api/earthquakes/us1/revisions/")

    assert detail.status_code == 410 and error(detail)["code"] == "gone"
    assert (
        history.status_code == 200
        and history.json()["results"][0]["status"] == "deleted"
    )


def test_expand_revisions_embeds_history_newest_first_in_fixed_queries(
    api, django_assert_max_num_queries
):
    q = quake("us1")
    for minutes, mag in ((0, 4.5), (5, 4.7), (10, 4.9)):
        EarthquakeRevision.objects.create(
            earthquake=q,
            updated=T0 + timedelta(minutes=minutes),
            mag=mag,
            status="reviewed",
            raw={},
        )

    with django_assert_max_num_queries(2):  # the event + one prefetch for all revisions
        response = api.get("/api/earthquakes/us1/", {"expand": "revisions"})

    assert [r["mag"] for r in response.json()["revisions"]] == [4.9, 4.7, 4.5]


#  method + throttling


def test_api_is_read_only(api):
    quake("us1")

    response = api.delete("/api/earthquakes/us1/")

    assert response.status_code in (401, 403, 405)
    assert Earthquake.objects.filter(usgs_id="us1").exists()


def test_anonymous_clients_are_throttled_with_a_retry_hint(api, monkeypatch):
    # DRF reads rates from settings once at import, so set the rate on the class.
    monkeypatch.setattr(AnonRateThrottle, "rate", "2/min", raising=False)

    statuses = [api.get(LIST).status_code for _ in range(3)]
    response = api.get(LIST)

    assert statuses == [200, 200, 429]
    assert error(response)["code"] == "throttled"
    assert "retry_after_seconds" in error(response)["details"]
