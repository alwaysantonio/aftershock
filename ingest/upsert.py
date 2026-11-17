"""
Raw SQL, deliberately. The rule we need is "insert, or overwrite only if the
incoming version is newer":

    INSERT ... ON CONFLICT (usgs_id) DO UPDATE SET ...
    WHERE quakes_earthquake.updated < EXCLUDED.updated

Django's bulk_create(update_conflicts=True) can't express that WHERE clause,
so it would let an old version overwrite a new one when batches arrive out
of order. Everything else in the project uses the ORM.

Guarantees:
- Running the same batch twice changes nothing the second time.
- An older version never overwrites a newer one.
- Each event's revision row is written in the same transaction as the event.
- A deletion that arrives with its fields stripped only updates an existing
  row; it never inserts a half-empty one.
"""

from dataclasses import dataclass
from datetime import datetime
from itertools import batched

from django.db import connection, transaction
from psycopg.types.json import Jsonb

from quakes.models import Earthquake, EarthquakeRevision

from .parsing import ParsedEvent

EQ = Earthquake._meta.db_table
REV = EarthquakeRevision._meta.db_table

BATCH_SIZE = 1_000

EVENT_COLUMNS = (
    "usgs_id",
    "time",
    "updated",
    "mag",
    "mag_type",
    "latitude",
    "longitude",
    "depth_km",
    "place",
    "status",
    "event_type",
    "tsunami",
    "sig",
    "is_deleted",
    "raw",
)
UPDATABLE = [c for c in EVENT_COLUMNS if c != "usgs_id"]

REVISION_COLUMNS = (
    "earthquake_id",
    "updated",
    "mag",
    "mag_type",
    "status",
    "latitude",
    "longitude",
    "depth_km",
    "raw",
)


@dataclass
class UpsertResult:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    newest_updated: datetime | None = None

    def __iadd__(self, other: UpsertResult) -> UpsertResult:
        self.inserted += other.inserted
        self.updated += other.updated
        self.unchanged += other.unchanged
        self.skipped += other.skipped
        if other.newest_updated and (
            self.newest_updated is None or other.newest_updated > self.newest_updated
        ):
            self.newest_updated = other.newest_updated
        return self


def upsert_events(
    events: list[ParsedEvent], batch_size: int = BATCH_SIZE
) -> UpsertResult:
    """
    Upsert events in batches, each batch in its own transaction.

    Expects events de-duplicated by usgs_id (parse_features does this):
    Postgres rejects an ON CONFLICT DO UPDATE that touches one row twice.
    """
    total = UpsertResult()
    for batch in batched(events, batch_size):
        with transaction.atomic():
            total += _upsert_batch(list(batch))
    return total


def _upsert_batch(events: list[ParsedEvent]) -> UpsertResult:
    result = UpsertResult(newest_updated=max(e.updated for e in events))
    complete = [e for e in events if e.is_complete]
    stripped = [e for e in events if not e.is_complete]

    with connection.cursor() as cur:
        if complete:
            inserted, updated = _upsert_complete(cur, complete)
            result.inserted, result.updated = inserted, updated
            result.unchanged = len(complete) - inserted - updated
        if stripped:
            applied = _apply_stripped_deletions(cur, stripped)
            known = _existing_ids(cur, [e.usgs_id for e in stripped])
            result.updated += applied
            result.unchanged += len(known) - applied
            result.skipped = len(stripped) - len(known)

        _record_revisions(cur, events)

    return result


def _upsert_complete(cur, events: list[ParsedEvent]) -> tuple[int, int]:
    row = "(" + ", ".join(["%s"] * len(EVENT_COLUMNS)) + ")"
    sql = f"""
        INSERT INTO {EQ} ({", ".join(EVENT_COLUMNS)})
        VALUES {", ".join([row] * len(events))}
        ON CONFLICT (usgs_id) DO UPDATE SET
            {", ".join(f"{c} = EXCLUDED.{c}" for c in UPDATABLE)},
            last_ingested_at = now()
        WHERE {EQ}.updated < EXCLUDED.updated
        RETURNING (xmax = 0) AS was_inserted
    """
    # (xmax = 0) is true for freshly inserted rows and false for updated ones.
    # Rows the WHERE clause skipped are not returned at all: those are "unchanged".
    cur.execute(sql, [value for e in events for value in _event_values(e)])
    flags = [was_inserted for (was_inserted,) in cur.fetchall()]
    inserted = sum(flags)
    return inserted, len(flags) - inserted


def _apply_stripped_deletions(cur, events: list[ParsedEvent]) -> int:
    rows = ", ".join(["(%s, %s::timestamptz, %s::jsonb)"] * len(events))
    sql = f"""
        UPDATE {EQ} AS e
        SET status = 'deleted', is_deleted = true, updated = v.updated,
            raw = v.raw, last_ingested_at = now()
        FROM (VALUES {rows}) AS v(usgs_id, updated, raw)
        WHERE e.usgs_id = v.usgs_id AND e.updated < v.updated
    """
    cur.execute(
        sql, [value for e in events for value in (e.usgs_id, e.updated, Jsonb(e.raw))]
    )
    return cur.rowcount


def _record_revisions(cur, events: list[ParsedEvent]) -> None:
    """
    One history row per version we've seen, for every event we hold.
    ON CONFLICT DO NOTHING on (earthquake_id, updated) makes re-runs harmless,
    and also records older versions that arrive late.
    """
    ids = _existing_ids(cur, [e.usgs_id for e in events])
    rows = [(ids[e.usgs_id], e) for e in events if e.usgs_id in ids]
    if not rows:
        return

    placeholder = "(" + ", ".join(["%s"] * len(REVISION_COLUMNS)) + ")"
    sql = f"""
        INSERT INTO {REV} ({", ".join(REVISION_COLUMNS)})
        VALUES {", ".join([placeholder] * len(rows))}
        ON CONFLICT (earthquake_id, updated) DO NOTHING
    """
    params = [
        value
        for pk, e in rows
        for value in (
            pk,
            e.updated,
            e.mag,
            e.mag_type,
            e.status,
            e.latitude,
            e.longitude,
            e.depth_km,
            Jsonb(e.raw),
        )
    ]
    cur.execute(sql, params)


def _existing_ids(cur, usgs_ids: list[str]) -> dict[str, int]:
    cur.execute(f"SELECT usgs_id, id FROM {EQ} WHERE usgs_id = ANY(%s)", [usgs_ids])
    return dict(cur.fetchall())


def _event_values(e: ParsedEvent) -> tuple:
    return (
        e.usgs_id,
        e.time,
        e.updated,
        e.mag,
        e.mag_type,
        e.latitude,
        e.longitude,
        e.depth_km,
        e.place,
        e.status,
        e.event_type,
        e.tsunami,
        e.sig,
        e.is_deleted,
        Jsonb(e.raw),
    )
