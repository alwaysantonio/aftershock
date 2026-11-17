"""Operations API and health check tests."""

from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from ingest import tasks
from ingest.models import IngestWindow, SyncState

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin_api():
    user = get_user_model().objects.create_user("admin", is_staff=True)
    client = APIClient()
    client.credentials(
        HTTP_AUTHORIZATION=f"Token {Token.objects.create(user=user).key}"
    )
    return client


def test_ops_endpoints_require_an_admin_token():
    anon = APIClient()

    assert anon.get("/api/ingest/windows/").status_code == 401
    assert anon.get("/api/ingest/status/").status_code == 401
    assert (
        anon.post(
            "/api/ingest/windows/", {"start": "2026-01-01", "end": "2026-01-02"}
        ).status_code
        == 401
    )
    assert not IngestWindow.objects.exists()


def test_backfill_request_creates_windows_and_queues_them_after_commit(
    admin_api, django_capture_on_commit_callbacks
):
    with mock.patch.object(tasks.ingest_window, "delay") as delay:
        with django_capture_on_commit_callbacks(execute=True):
            response = admin_api.post(
                "/api/ingest/windows/",
                {"start": "2026-01-01", "end": "2026-01-04"},
                format="json",
            )

    assert response.status_code == 202
    assert response.json()["queued"] == 3
    assert IngestWindow.objects.count() == 3
    assert delay.call_count == 3
    assert SyncState.objects.exists()


def test_backfill_request_is_validated(admin_api):
    response = admin_api.post(
        "/api/ingest/windows/",
        {"start": "2026-02-01", "end": "2026-01-01"},
        format="json",
    )

    assert response.status_code == 400
    assert "end" in response.json()["error"]["details"]


def test_status_summarises_both_jobs(admin_api):
    IngestWindow.objects.create(
        start="2026-01-01T00:00Z", end="2026-01-02T00:00Z", status="succeeded"
    )
    IngestWindow.objects.create(
        start="2026-01-02T00:00Z", end="2026-01-03T00:00Z", status="failed"
    )

    body = admin_api.get("/api/ingest/status/").json()

    assert body["windows"] == {"succeeded": 1, "failed": 1}
    assert body["sync"] is None and body["rejected_records"] == 0


def test_healthz_reports_each_dependency(client):
    with mock.patch("config.health._check_broker", return_value="ok"):
        healthy = client.get("/healthz")
    with mock.patch("config.health._check_broker", return_value="error"):
        degraded = client.get("/healthz")

    assert healthy.status_code == 200 and healthy.json()["checks"] == {
        "database": "ok",
        "broker": "ok",
    }
    assert degraded.status_code == 503 and degraded.json()["status"] == "degraded"
