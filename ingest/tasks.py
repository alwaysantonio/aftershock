import logging
import random
from datetime import UTC, datetime

from celery import shared_task
from django.conf import settings
from django.db import transaction

from . import services
from .client import PermanentError, TransientError, USGSClient

log = logging.getLogger(__name__)


def make_client() -> USGSClient:
    return USGSClient(
        settings.USGS_BASE_URL, read_timeout=settings.USGS_TIMEOUT_SECONDS
    )


def retry_delay(
    retries: int, retry_after: float | None, base: float = 30, cap: float = 600
) -> float:
    """Exponential backoff with jitter (30s, 60s, 120s... up to 10 min), or the server's Retry-After."""
    if retry_after is not None:
        return min(retry_after, cap)
    return min(cap, base * 2**retries) * random.uniform(0.5, 1.5)


@shared_task(bind=True, max_retries=5)
def ingest_window(self, window_id: int) -> dict:
    """Backfill one window. Retries transient failures with a long backoff."""
    with make_client() as client:
        try:
            outcome = services.run_window(window_id, client)
        except TransientError as exc:
            final = self.request.retries >= self.max_retries
            services.record_window_error(window_id, exc, final=final)
            if final:
                log.error(
                    "window %s failed after %s retries: %s",
                    window_id,
                    self.max_retries,
                    exc,
                )
                return {"window": window_id, "status": "failed"}
            raise self.retry(
                exc=exc, countdown=retry_delay(self.request.retries, exc.retry_after)
            )

    enqueue_windows(outcome.children)
    return {"window": window_id, "status": outcome.status}


@shared_task(bind=True, max_retries=2)
def sync_recent_changes(self) -> dict:
    """
    Scheduled by beat every SYNC_INTERVAL_SECONDS. Few retries on purpose: the
    next scheduled run is only minutes away and resumes from the same watermark.
    """
    history_start = datetime.fromisoformat(settings.INGEST_HISTORY_START).replace(
        tzinfo=UTC
    )
    with make_client() as client:
        try:
            outcome = services.run_sync(client, history_start=history_start)
        except TransientError as exc:
            if self.request.retries >= self.max_retries:
                log.warning("sync gave up until the next scheduled run: %s", exc)
                return {"status": "failed"}
            raise self.retry(
                exc=exc,
                countdown=retry_delay(
                    self.request.retries, exc.retry_after, base=20, cap=120
                ),
            )
        except PermanentError as exc:
            log.error("sync failed permanently: %s", exc)
            return {"status": "failed"}

    return {
        "status": "succeeded",
        "fetched": outcome.fetched,
        "inserted": outcome.result.inserted,
        "updated": outcome.result.updated,
        "watermark": outcome.watermark.isoformat(),
    }


def enqueue_windows(window_ids: list[int]) -> None:
    """
    Enqueue after the surrounding transaction commits (immediately if there is
    none), so a worker never picks up a window id whose row isn't visible yet.
    """
    if window_ids:
        ids = list(window_ids)
        transaction.on_commit(lambda: [ingest_window.delay(i) for i in ids])
