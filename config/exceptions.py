import logging

from django.conf import settings
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.http import Http404
from rest_framework import exceptions, status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

log = logging.getLogger(__name__)

# client
MAX_EVENTS_PER_QUERY = 20_000


class USGSError(Exception):
    """Base class for client errors."""


class TransientError(USGSError):
    """Worth retrying later: timeouts, connection errors, 429, 5xx."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class PermanentError(USGSError):
    """Retrying won't help: a bad request, an unexpected response shape."""


class WindowTooLarge(USGSError):
    """The query matches more events than USGS will return. Split it."""

    def __init__(self, count: int):
        super().__init__(
            f"{count} events exceeds the {MAX_EVENTS_PER_QUERY} per-query limit"
        )
        self.count = count


# API


class Gone(exceptions.APIException):
    """410: the resource existed but was withdrawn (USGS deleted the event)."""

    status_code = status.HTTP_410_GONE
    default_detail = "This resource has been withdrawn."
    default_code = "gone"


def error_body(code: str, message: str, details=None) -> dict:
    return {"error": {"code": code, "message": message, "details": details or {}}}


def exception_handler(exc, context):
    # Django's own 404/403 carry no DRF error code; translate them first so
    # they get the same stable codes ("not_found", "permission_denied").
    if isinstance(exc, Http404):
        exc = exceptions.NotFound()
    elif isinstance(exc, DjangoPermissionDenied):
        exc = exceptions.PermissionDenied()

    response = drf_exception_handler(exc, context)

    if response is None:
        # Not a DRF/HTTP error: a bug. In DEBUG let Django show the traceback;
        # otherwise answer in the same JSON shape instead of an HTML page.
        if settings.DEBUG:
            return None
        log.exception("unhandled error in %s", context.get("view"))
        return Response(
            error_body("server_error", "Something went wrong on our side."),
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    if isinstance(exc, exceptions.ValidationError):
        response.data = error_body(
            "invalid", "Request parameters are invalid.", response.data
        )
        return response

    detail = getattr(exc, "detail", None)
    code = getattr(detail, "code", None) or getattr(exc, "default_code", "error")
    details = {}
    if isinstance(exc, exceptions.Throttled) and exc.wait is not None:
        details["retry_after_seconds"] = int(exc.wait)
    response.data = error_body(
        str(code), str(detail if detail is not None else exc), details
    )
    return response
