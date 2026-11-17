from django.core.management.base import BaseCommand
from django.db import transaction

from ingest import services
from ingest.tasks import enqueue_windows


class Command(BaseCommand):
    help = "First-start setup: create the sync bookmark and optionally queue an initial backfill."

    def add_arguments(self, parser):
        parser.add_argument("--backfill-days", type=int, default=0)
        parser.add_argument(
            "--if-empty",
            action="store_true",
            help="Only backfill if no data or windows exist yet.",
        )

    def handle(self, *, backfill_days, if_empty, **_):
        with transaction.atomic():
            ids = services.bootstrap(backfill_days=backfill_days, if_empty=if_empty)
            enqueue_windows(ids)
        self.stdout.write(
            self.style.SUCCESS(f"Bootstrap done; {len(ids)} backfill windows queued.")
        )
