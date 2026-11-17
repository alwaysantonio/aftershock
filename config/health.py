import logging

from django.db import connection
from django.http import JsonResponse

from .celery import app as celery_app

log = logging.getLogger(__name__)


def healthz(request):
    checks = {"database": _check_database(), "broker": _check_broker()}
    healthy = all(result == "ok" for result in checks.values())
    return JsonResponse(
        {"status": "ok" if healthy else "degraded", "checks": checks},
        status=200 if healthy else 503,
    )


def _check_database() -> str:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        return "ok"
    except Exception:
        log.exception("health check: database unreachable")
        return "error"


def _check_broker() -> str:
    try:
        with celery_app.connection_for_write() as conn:
            conn.ensure_connection(max_retries=1, timeout=2)
        return "ok"
    except Exception:
        log.exception("health check: broker unreachable")
        return "error"
