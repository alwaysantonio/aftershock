from datetime import UTC, datetime

import httpx
import pytest
import respx

from ingest.client import (
    MAX_EVENTS_PER_QUERY,
    EventQuery,
    PermanentError,
    TransientError,
    USGSClient,
    WindowTooLarge,
)

BASE = "https://usgs.test/fdsnws/event/1"
START = datetime(2026, 1, 1, tzinfo=UTC)
END = datetime(2026, 1, 2, tzinfo=UTC)
FEATURE = {"type": "Feature", "id": "us1", "properties": {}, "geometry": {}}


@pytest.fixture
def sleeps():
    return []


@pytest.fixture
def client(sleeps):
    c = USGSClient(BASE, max_attempts=3, sleep=sleeps.append)
    yield c
    c.close()


@pytest.fixture
def usgs():
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        yield mock


def test_fetch_returns_features(client, usgs):
    usgs.get("/count").respond(200, json={"count": 1})
    usgs.get("/query").respond(200, json={"features": [FEATURE]})

    assert client.fetch(EventQuery(START, END)) == [FEATURE]


def test_sync_query_always_sends_starttime_and_includedeleted(client, usgs):
    usgs.get("/count").respond(200, json={"count": 1})
    query_route = usgs.get("/query").respond(200, json={"features": [FEATURE]})

    client.fetch(EventQuery(starttime=START, updatedafter=END, include_deleted=True))

    params = query_route.calls.last.request.url.params
    assert params["starttime"] == "2026-01-01T00:00:00"
    assert params["updatedafter"] == "2026-01-02T00:00:00"
    assert params["includedeleted"] == "true"


def test_oversized_window_raises_without_fetching(client, usgs):
    usgs.get("/count").respond(200, json={"count": MAX_EVENTS_PER_QUERY + 1})
    query_route = usgs.get("/query")

    with pytest.raises(WindowTooLarge) as exc:
        client.fetch(EventQuery(START, END))

    assert exc.value.count == MAX_EVENTS_PER_QUERY + 1
    assert not query_route.called


def test_retries_transient_failure_then_succeeds(client, usgs, sleeps):
    usgs.get("/count").mock(
        side_effect=[
            httpx.ConnectTimeout("slow"),
            httpx.Response(503),
            httpx.Response(200, json={"count": 0}),
        ]
    )

    assert client.fetch(EventQuery(START, END)) == []
    assert len(sleeps) == 2  # backed off between the three attempts


def test_honours_retry_after_on_429(client, usgs, sleeps):
    usgs.get("/count").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(200, json={"count": 0}),
        ]
    )

    client.fetch(EventQuery(START, END))

    assert sleeps == [7.0]


def test_gives_up_after_max_attempts_with_transient_error(client, usgs):
    route = usgs.get("/count").respond(503)

    with pytest.raises(TransientError):
        client.fetch(EventQuery(START, END))

    assert route.call_count == 3  # the Celery task decides what happens next


def test_does_not_retry_bad_request(client, usgs, sleeps):
    route = usgs.get("/count").respond(400, text="Bad Request: bad parameter")

    with pytest.raises(PermanentError):
        client.fetch(EventQuery(START, END))

    assert route.call_count == 1
    assert sleeps == []


def test_malformed_payload_is_permanent(client, usgs):
    usgs.get("/count").respond(200, json={"count": 1})
    usgs.get("/query").respond(200, json={"type": "FeatureCollection"})  # no features

    with pytest.raises(PermanentError):
        client.fetch(EventQuery(START, END))


def test_rejects_naive_datetimes():
    with pytest.raises(ValueError):
        EventQuery(datetime(2026, 1, 1)).params()
