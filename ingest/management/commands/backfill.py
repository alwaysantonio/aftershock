from datetime import UTC, date, datetime, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from ingest import services
from ingest.tasks import enqueue_windows


def utc_date(value: str) -> datetime:
    return datetime.combine(date.fromisoformat(value), datetime.min.time(), tzinfo=UTC)


class Command(BaseCommand):
    help = "Queue one-day backfill windows for a date range. Safe to re-run: finished days are skipped."

    def add_arguments(self, parser):
        parser.add_argument(
            "--from", dest="start", type=utc_date, required=True, help="YYYY-MM-DD"
        )
        parser.add_argument(
            "--to",
            dest="end",
            type=utc_date,
            help="YYYY-MM-DD, exclusive (default: tomorrow)",
        )

    def handle(self, *, start, end, **_):
        end = end or utc_date(
            (datetime.now(UTC) + timedelta(days=1)).date().isoformat()
        )
        if end <= start:
            raise CommandError("--to must be after --from")
        with transaction.atomic():
            services.bootstrap(
                backfill_days=0, if_empty=False
            )  # make sure the sync bookmark exists
            ids = services.create_windows(start, end)
            enqueue_windows(ids)
        self.stdout.write(
            self.style.SUCCESS(
                f"{len(ids)} windows queued ({start:%Y-%m-%d} to {end:%Y-%m-%d})."
            )
        )
