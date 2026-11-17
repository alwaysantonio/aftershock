from django.utils import timezone
from datetime import datetime
from .models import IngestWindow, RejectedRecord
from .parsing import Rejection


def _set_status(window_id: int, status: str, *, error: str = "") -> None:
    IngestWindow.objects.filter(pk=window_id).update(
        status=status, last_error=error[:2000], finished_at=timezone.now()
    )


def _save_rejections(
    rejections: list[Rejection], origin: str, window: IngestWindow | None = None
) -> None:
    if rejections:
        RejectedRecord.objects.bulk_create(
            RejectedRecord(
                origin=origin,
                window=window,
                usgs_id=r.usgs_id,
                reason=r.reason[:255],
                payload=r.payload,
            )
            for r in rejections
        )


def _midnight(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)
