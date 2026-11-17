# aftershock

Ingests the [USGS earthquake catalogue](https://earthquake.usgs.gov/fdsnws/event/1/) into PostgreSQL on a schedule and serves it through a fast, filterable, paginated API.

The upstream API is rate-limited, capped at 20,000 results per query, and revises events after publishing them. aftershock keeps a clean local copy that stays in sync safely: re-runs, crashes, duplicates, late revisions and deletions are all expected and handled.

---

## How I used AI

- I used Claude to help write the README.
- Writing tests 
- Admin pages 
- Infra related to the project (docker, docker-compose, makefile, etc.)
- New to Celery, so I used Claude to help me understand how to use it and how to write tasks.
- Summarising the USGS earthquake data source and its quirks
- Parsing of the data and writing the upsert logic to handle revisions and deletions
- Creating the query plans docs/query-plans.md **[docs/query-plans.md](docs/query-plans.md)**
- Discussing the benefits of using a watermark for the sync rather than `SELECT MAX(updated)` from the table.
- Discussing the benefits of using raw SQL for the upsert rather than Django's `bulk_create(update_conflicts=True)`.
- Benefits of using httpx over requests for the client for this project. Default timeouts, retry logic, and the ability to inject a fake transport for testing.
- Refactoring Code for better readability and maintainability.

---

## Track and data source

**Track 2: ingest and serve at scale**, using the **USGS FDSN Event Web Service**.

Why USGS:

- **Free and keyless**, run by a reputable public body.
- **Time-windowed queries** (`starttime` / `endtime`) let ingestion be split into small, independently retryable units.
- **A stable natural key** (`id`, e.g. `us7000n5no`) and an **`updated` timestamp** on every event make an idempotent, newest-wins upsert possible.
- **The data really changes.** Events are published automatically, then revised (magnitude corrected, status becomes `reviewed`) or deleted. Re-runs and conflicting versions happen in practice, not just in theory.
- **Incremental sync is supported** via `updatedafter`, and `/count` lets us size a window before fetching it.
- **It has a hard limit to design around:** a query matching more than 20,000 events returns `400 Bad Request` rather than truncating.

GLEIF (millions of legal-entity records) was the alternative. USGS was chosen because it can be finished properly in the timebox, while still producing a realistic "messy, changing data" problem.

---

## Quick start

Requires Docker. No API keys, accounts or manual setup.

```bash
make waves

or 

docker compose up --build
```

On first start this:

1. starts Postgres 16 and Redis,
2. runs migrations once, in a one-off `migrate` service,
3. enqueues a 30-day backfill if the database is empty,
4. starts the API on <http://localhost:8000>, a Celery worker, and Celery beat (sync every 5 minutes).

### Running the tests

```bash
make test
```

The upsert tests need Postgres (the SQL is Postgres-specific); the client and parser tests are pure Python and need nothing.

### Example requests

```bash
# Latest events (cursor-paginated, newest first)
curl -s "localhost:8000/api/earthquakes/" | jq

# Magnitude 5+ in January 2026, around Japan
curl -s "localhost:8000/api/earthquakes/?min_mag=5&start=2026-01-01&end=2026-02-01&bbox=128,30,146,46" | jq

# Next page: follow the "next" link from the previous response
curl -s "<next url>" | jq

# One event, with its revision history embedded
curl -s "localhost:8000/api/earthquakes/<usgs_id>/?expand=revisions" | jq

# Only events USGS changed since a time (lets clients sync from us incrementally)
curl -s "localhost:8000/api/earthquakes/?updated_since=2026-11-16T00:00:00Z" | jq

# Validation error, in the standard error shape
curl -s "localhost:8000/api/earthquakes/?min_mag=6&max_mag=5" | jq

# Operations (admin token; dev default shown)
curl -s -H "Authorization: Token dev-admin-token" "localhost:8000/api/ingest/status/" | jq
curl -s -H "Authorization: Token dev-admin-token" "localhost:8000/api/ingest/windows/?status=failed" | jq
curl -s -X POST -H "Authorization: Token dev-admin-token" -H "Content-Type: application/json" \
     -d '{"start": "2025-12-01", "end": "2026-01-01"}' "localhost:8000/api/ingest/windows/" | jq

# Health
curl -s "localhost:8000/healthz" | jq
```

The browsable API is at <http://localhost:8000/api/>; the Django admin at <http://localhost:8000/admin/> (dev login `admin` / `admin`).

---

## Architecture

```
Celery beat ──► sync task (every 5 min, updatedafter watermark)  ─┐
                                                                  ├─► USGS client ──► parser ──► upsert ──► Postgres
backfill cmd ──► one task per time window (split if count > 20k) ─┘   (timeouts,      (validate,   (newest wins,     ▲
                                                                       retries)        reject)      + history)       │
                                                                                                                     │
                                              DRF API (gunicorn): filters, cursor pagination, read-only ─────────────┘
```

**Both write paths share one pipeline** (client → parser → upsert), so there is one thing to make safe and one thing to test. **The API never calls USGS** on the request path; it only reads Postgres.

### Services (`docker-compose.yml`)

| Service | Role |
|---|---|
| `db` | Postgres 16, named volume, healthcheck. Port 5432 exposed for running `EXPLAIN ANALYZE` locally. |
| `redis` | Celery broker. |
| `migrate` | One-shot: migrations, seed `SyncState`, initial backfill if empty. |
| `web` | Gunicorn serving Django/DRF. |
| `worker` | Celery worker running ingestion tasks. |
| `beat` | Celery beat; enqueues the sync every 5 minutes. |

Decisions:

- **Migrations run in their own one-shot service.** If `web`, `worker` and `beat` each ran `migrate` on start, they would race. Everything else waits for `migrate` with `service_completed_successfully`.
- **Startup is ordered by healthchecks**, not sleeps.
- **`docker compose up` is idempotent:** the initial backfill only runs when the table is empty.
- **Gunicorn, not `runserver`.** `runserver` is development-only (single process, not hardened); Gunicorn gives multiple workers, request timeouts and graceful restarts. The service is ordinary sync DRF, so WSGI is the simpler fit than ASGI/Uvicorn.
- **Non-root container user.**

### Code layout

```
config/            Django project: settings (all from env), urls, wsgi, celery
quakes/            The domain and the API. Knows nothing about USGS.
  models.py        Earthquake, EarthquakeRevision
ingest/            Everything that talks to USGS.
  models.py        IngestWindow, SyncState, RejectedRecord
  client.py        HTTP: timeouts, retries, error classification
  parsing.py       Raw GeoJSON feature → clean event, or a rejection with a reason
  upsert.py        Conditional ON CONFLICT upsert + revision history
  services.py      The workflows: run a backfill window, run the sync, bootstrap
  tasks.py         Thin Celery wrappers: retries, backoff, scheduling
  management/      bootstrap and backfill commands
```

**`ingest` depends on `quakes`; `quakes` never depends on `ingest`.** Swapping USGS for another source would only touch `ingest`.

### Tooling

- **Python 3.14, Django 6.0, DRF, Celery 5 + Redis, PostgreSQL 16.**
- **uv** with a committed `uv.lock`. The Docker build uses `uv sync --locked`, which fails if the lock file is out of date, so reviewers run exactly the versions that were tested. Dependencies install in their own cached layer.
- **httpx** rather than requests. Mainly because requests has *no timeout by default* (one forgotten `timeout=` and a worker hangs forever); httpx has default and per-phase timeouts, distinct timeout vs transport errors (which map onto our retry logic), injectable transports for clean tests, and the same API in async if concurrency is ever needed. requests would also have worked.

---

## Data model

### `quakes` app

**`Earthquake`**: one row per USGS event, always holding its **latest** version.

| Field | Notes |
|---|---|
| `id` | Internal `bigint` primary key. Never exposed. |
| `usgs_id` | Unique natural key; the only identifier the API exposes. |
| `time`, `updated` | When the quake happened; when USGS last revised it. |
| `mag`, `mag_type`, `depth_km`, `sig` | Nullable: fresh automatic solutions can lack them. |
| `latitude`, `longitude` | Required for stored events, with database `CHECK` constraints on range. |
| `status`, `event_type`, `tsunami`, `place` | |
| `is_deleted` | Soft delete; deleted events are hidden but kept. |
| `raw` | The full source feature (JSONB), so nothing is lost. |

**`EarthquakeRevision`**: append-only history, one row per distinct version seen. A unique constraint on `(earthquake, updated)` makes it idempotent.

### `ingest` app

| Model | Purpose | Rows |
|---|---|---|
| **`IngestWindow`** | The **backfill's to-do list**: one row per slice of *event time* (`start`, `end`), with status, attempts, counts and last error. Unique `(start, end)` so the backfill command can be re-run safely. | Many; finished once |
| **`SyncState`** | The **ongoing sync's bookmark**: `watermark` = the highest USGS `updated` value successfully saved. | One per source; updated forever |
| **`RejectedRecord`** | Quarantine for features that failed validation: reason, payload, and the window that produced it. | Grows with bad data |

### Decisions

- **`bigint` primary key, not UUID.** The public identifier is already `usgs_id`, so there's no need to hide sequential IDs. A bigint is half the size of a UUID in every index and foreign key, and appends to the end of the primary-key index; random UUID4s scatter inserts across it, causing page splits under bulk ingestion. If opaque public IDs were ever needed, UUIDv7 (time-ordered, `uuid.uuid7()` in Python 3.14) would be the choice.
- **Partial indexes on `is_deleted = false`.** The API never reads deleted rows, so its indexes skip them. Consequence: API queries must filter on `is_deleted=False`, which is why everything goes through `Earthquake.objects.visible()`.
- **Indexes:** `(time DESC, id DESC)` for the default sort and keyset pagination; `(mag, time)` for "big quakes in a date range"; `(latitude, longitude)` for bounding boxes. PostGIS with a GiST index is the production upgrade for geographic queries, left out to keep the moving parts down.
- **Database `CHECK` constraints** on latitude, longitude and magnitude ranges, so a parser bug can't store impossible data.
- **`db_default=Now()` instead of `auto_now`.** Writes go through raw SQL, and `auto_now` only fires through the ORM's `save()`, so it would silently never run.
- **Two separate progress models** rather than one `IngestRun` table with a `kind` column. They answer different questions ("which dates of history are loaded?" vs "how recent a change have we seen?"), and the single `SyncState` row doubles as a lock (`select_for_update()`), so two syncs can never run at once. A merged table with a sync audit log is a valid alternative.

---

## Ingestion design

There are **two jobs**, deliberately tracked by two different timestamps.

### 1. Backfill: historical data, run once

```bash
docker compose run --rm web python manage.py backfill --from 2014-01-01 --to 2026-11-17
```

- Splits the range into one-day `IngestWindow`s aligned to midnight UTC, tracked by **event time**.
- One Celery task (`ingest_window`) per window. Windows are independent: day 47 can fail and be retried while day 48 succeeds.
- Before fetching, the client calls `/count`. A window over 20,000 events is marked `split` and replaced by two half windows, recursively down to one minute (USGS would return `400` otherwise).
- **Re-running the command is safe:** existing windows aren't recreated, finished ones are skipped, and previously failed ones are queued again.
- **Concurrent workers:** the write takes `select_for_update(skip_locked=True)` on the window row, so if two workers ever hold the same window, one writes and the other skips.
- USGS's `endtime` is inclusive, so an event exactly on a boundary can be fetched by two windows. The upsert makes that harmless.
- Task ids are enqueued with `transaction.on_commit`, so a worker never receives an id for a window row it can't see yet.

### 2. Sync: keep current, forever

Every 5 minutes, Celery beat enqueues `sync_recent_changes`, which:

1. asks USGS for `updatedafter = watermark − 10 minutes`, with `includedeleted=true` and an explicit `starttime` (`INGEST_HISTORY_START`, default 2000-01-01). **This HTTP fetch happens before any lock**, so a slow USGS response never holds a database lock,
2. then, in one transaction, locks the `SyncState` row, upserts what came back, and
3. **in that same transaction**, moves the watermark to the highest `updated` value saved. It never moves backwards.

If two syncs ever overlap, both writes are idempotent and the watermark takes the higher value, so the worst case is wasted work, never lost or duplicated data. If more than 20,000 events changed (e.g. after a long outage), the sync splits the *event-time* range in half until each part fits, keeping the same `updatedafter`.

The beat entry has `expires` equal to the interval: if workers are down, missed runs don't pile up and stampede on recovery, since each would re-fetch from the same watermark anyway.

**Why "what changed since X?" and not "what happened today?"** USGS revises old events. A quake from last week corrected today has an event date long before today, so a date-based daily job would never see the correction. `updatedafter` returns new events *and* revisions to old ones.

**Why the watermark comes from the data, not the clock.** Storing `now()` would open gaps from clock skew between us and USGS, or from events indexed slightly late. Taking the highest `updated` we actually saved means we can only ever re-fetch, never skip.

**Why the 10-minute overlap.** It covers late-indexed events. Fetching some events twice is harmless because the upsert is idempotent.

**Why not `SELECT MAX(updated) FROM earthquake`.** During a backfill, a window fetched later can contain very recent `updated` values, pushing the max past revisions to events in windows fetched earlier. Those revisions would be skipped. The watermark must come from the sync's own successful runs.

**How the two jobs connect.** On first deploy, the watermark is set to when the backfill started (minus the overlap), so anything revised while the backfill was running is picked up by the first sync.

**Why `starttime` is always sent.** USGS appears to default `starttime` to 30 days ago. Without it, an `updatedafter` query would silently skip revisions to older events. *(To verify against the docs; a test pins that we always send it.)*

**Recovery after an outage needs no special procedure.** The watermark hasn't moved, so the next run asks for everything since the last good point (split if it exceeds 20,000).

### Deletions

USGS **excludes deleted events from query results by default**. If the sync used the default, a deleted event would silently stop appearing, and we would keep serving it forever with no error. So the sync sends **`includedeleted=true`**, and a deletion arrives as an ordinary change: a newer `updated` with `status: "deleted"`.

- A deletion with full data goes through the normal upsert with `is_deleted = true`.
- A deletion **with its fields stripped** only updates an existing row (status, `updated`, `is_deleted`), keeping its last known location. It never inserts a half-empty row.
- A deletion for an event we never stored is skipped.
- The deletion is recorded in `EarthquakeRevision`, so history shows when the event was withdrawn.
- USGS's single-event lookup returns `409 Conflict` for deleted events; the client treats that as permanent, not retryable.
- *Planned:* the API returns `410 Gone` for a deleted event's detail URL, telling clients it existed and was withdrawn.

---

## Resilience

### HTTP client (`ingest/client.py`)

- **Explicit timeouts:** 5 s connect, 30 s read.
- **Errors are classified:**
  - `TransientError` (worth retrying): timeouts, connection errors, `429`, `5xx`.
  - `PermanentError` (retrying won't help): other `4xx`, invalid JSON, a response with no `features`.
  - `WindowTooLarge`: the count exceeds 20,000. Raised *before* fetching, so the caller splits the window.
- **Two retry layers, deliberately.** The client retries brief blips up to 3 times within seconds (exponential backoff with full jitter, honouring `Retry-After`). Longer outages surface as `TransientError`, and the Celery task retries later with a longer backoff. Short in-process retries stop a worker being held for minutes, and stop the two layers multiplying into a retry storm.
- **No Django dependency**, an identifying `User-Agent`, deterministic ordering (`orderby=time-asc`), and naive datetimes refused.

### Bad data (`ingest/parsing.py`)

Every feature becomes exactly one of a clean `ParsedEvent` or a `Rejection` with a readable reason (e.g. `latitude out of range [-90.0, 90.0]: 91.2`), which is stored in `RejectedRecord`. The batch carries on without it.

| Situation | Policy |
|---|---|
| Missing optional value (magnitude, depth, place, significance) | Kept as empty. Fresh automatic events legitimately lack them. |
| Impossible value (latitude 91, magnitude 14, NaN, string where a number belongs, unknown status) | **Reject.** The payload is corrupt; guessing would serve wrong data. |
| Over-long text | Truncated, not rejected. A long place name is no reason to lose an event. |
| Deletion with fields stripped | Accepted; flagged so the upsert only updates. |

Bounds are generous on purpose (depth −10 to 1,000 km, magnitude −2 to 10): reject the impossible, not the unusual. Booleans are explicitly rejected where numbers are expected, because in Python `isinstance(True, int)` is true, so `"mag": true` would otherwise become magnitude 1.0.

**The parser de-duplicates each batch by `usgs_id`, keeping the newest version.** Overlapping sync windows can return the same event twice, and Postgres refuses an `ON CONFLICT DO UPDATE` that touches one row twice in a single statement.

### Idempotent upsert (`ingest/upsert.py`)

```sql
INSERT INTO quakes_earthquake (...) VALUES (...), (...)
ON CONFLICT (usgs_id) DO UPDATE SET ...
WHERE quakes_earthquake.updated < EXCLUDED.updated
RETURNING (xmax = 0) AS was_inserted
```

- **Running the same batch twice changes nothing.** An older or identical version is a no-op, so out-of-order batches can never overwrite fresh data with stale data.
- **Why raw SQL:** Django's `bulk_create(update_conflicts=True)` can't express the "only if newer" `WHERE` clause. Everything else in the project uses the ORM.
- **`RETURNING (xmax = 0)`** distinguishes inserted from updated rows in a single statement. It relies on a Postgres implementation detail, but a stable and widely used one.
- **Revisions are written in the same transaction**, with `ON CONFLICT (earthquake_id, updated) DO NOTHING`, so history is never duplicated, and older versions that arrive late are still recorded.
- **Batches of 1,000 rows, each in its own transaction**, so a failure loses one batch, not a whole window, and the statement stays well under Postgres's 65,535-parameter limit.
- Returns inserted, updated, unchanged and skipped counts (for `IngestWindow`) and `newest_updated` (for the watermark).

### Task-level guarantees (`ingest/tasks.py`, `ingest/services.py`)

- **Workflows live in `services.py` as plain functions** that take a client; tasks only add retries and scheduling. That keeps the logic testable without a broker or network.
- **`acks_late=True` and `reject_on_worker_lost`:** a task whose worker dies is re-delivered. Safe because every step is idempotent.
- **`worker_prefetch_multiplier=1`:** a dying worker doesn't take a queue of reserved tasks with it.
- **Time limits** (soft 240 s, hard 300 s): a stuck task is interrupted rather than holding a worker forever.
- **Window retries:** transient failures put the window back to `pending` and retry up to 5 times with exponential backoff and jitter (30 s, 60 s, 120 s… capped at 10 min, or the server's `Retry-After`). After the last retry the window is marked `failed`, and re-running the backfill command queues it again. Permanent errors fail immediately.
- **Sync retries:** only 2, with short backoff. The next scheduled run is minutes away and resumes from the same watermark.
- **Progress is recorded in the same transaction as the data it describes**, so status and data can never disagree.

---

## API

| Method | Path | Access | Notes |
|---|---|---|---|
| GET | `/api/earthquakes/` | Public, throttled | Filters below; cursor-paginated, newest first |
| GET | `/api/earthquakes/{usgs_id}/` | Public, throttled | `?expand=revisions` embeds the newest 50 versions. **`410 Gone`** if USGS deleted the event |
| GET | `/api/earthquakes/{usgs_id}/revisions/` | Public, throttled | Full history, paginated; still readable for deleted events |
| GET | `/api/ingest/windows/` | Admin token | Backfill progress; `?status=failed` |
| POST | `/api/ingest/windows/` | Admin token | `{"start", "end"}` queues a backfill; returns `202 Accepted` |
| GET | `/api/ingest/status/` | Admin token | Sync watermark, last run and error; window counts by status; rejected records |
| GET | `/healthz` | Public | `200` if database and broker are reachable, `503` otherwise |

**List filters** (all validated; bad input returns `400`):

| Parameter | Meaning |
|---|---|
| `start`, `end` | Event time, ISO 8601 (`end` exclusive) |
| `min_mag`, `max_mag` | Magnitude range, −2 to 10 |
| `bbox` | `min_lon,min_lat,max_lon,max_lat` |
| `status` | `automatic` or `reviewed` |
| `type` | e.g. `earthquake`, `quarry blast` (case-insensitive) |
| `updated_since` | Only events USGS changed at or after this time |
| `page_size` | Default 50, max 500 |

### Decisions

- **Cursor (keyset) pagination** ordered by `(time, id)`, not offset: it stays fast at any depth, and pages never skip or repeat rows when new events arrive between requests. There's deliberately no total count, because `COUNT(*)` on a large table is slow. A test inserts a new event between page requests and checks that nothing is duplicated or skipped.
- **The list returns latest versions only.** Embedding history per row would multiply the payload and invite N+1 queries. History is on the detail endpoint (`?expand=revisions`, one extra prefetch query, capped at 50) or its own paginated resource.
- **No N+1 queries, proven by tests:** the list is exactly one query for any page size (cursor pagination needs no count query), and detail with expanded history is two.
- **`raw` is deferred** on every API query. It's the largest column and never shown; the serializer must never touch it, or each row would trigger an extra query.
- **`410 Gone` for deleted events** instead of `404`: it tells clients the event existed and was withdrawn, which matters for anyone who cached it. History stays readable, so the deletion itself is visible.
- **`usgs_id` is the only identifier exposed.** The internal bigint key never appears in URLs or responses.
- **`updated_since` filter:** lets downstream clients sync from us incrementally, the same way we sync from USGS.
- **One error shape** everywhere, via a custom DRF exception handler, including validation errors, 404s, 410s, auth failures and throttling:

  ```json
  {"error": {"code": "invalid", "message": "Request parameters are invalid.",
             "details": {"max_mag": ["must not be less than min_mag"]}}}
  ```

  Throttled responses include `details.retry_after_seconds`. With `DEBUG` off, unexpected server errors also answer in this shape instead of an HTML page.
- **Security:**
  - Earthquake endpoints are read-only (`ReadOnlyModelViewSet`); operations endpoints require an admin (`IsAdminUser`) via **token auth** (`Authorization: Token <key>`), or a session after logging into `/admin/`.
  - **Throttling**: 120 requests/min for anonymous clients, 600/min for authenticated ones. The counters live in Redis (`CACHE_URL`), so limits hold across all Gunicorn workers rather than per process.
  - All input is validated before it reaches the database: filter values and ranges, `bbox` shape and bounds, cross-field checks (`min_mag ≤ max_mag`, `start ≤ end`), and backfill ranges (at most 10 years per request).
  - The admin user and token are created idempotently by `ensure_admin` from `DJANGO_ADMIN_PASSWORD` / `DJANGO_ADMIN_TOKEN`. The Compose file sets labelled dev-only values so reviewers can try the operations endpoints with no setup; a real deployment sets them from a secret store, or leaves them unset (the command then does nothing).
- **`/healthz` is a plain Django view**: no auth or throttling, so Compose and load balancers can always reach it. It checks both the database and the broker and returns `503` if either is down.

## Performance evidence

Full write-up with plans: **[docs/query-plans.md](docs/query-plans.md)**

## Testing and QA

Tests target the logic that would cause damage if it broke, not coverage numbers. Current suite (73 tests):

- **Client:** returns features; always sends `starttime` and `includedeleted` on syncs; an oversized window raises without fetching; retries transient failures; honours `Retry-After`; gives up after max attempts; never retries a `400`; malformed payloads are permanent; naive datetimes are refused.
- **Parser:** valid features map correctly; missing optional values are kept; ten kinds of impossible value are rejected with reasons; a missing id is rejected with the payload kept; stripped deletions are accepted but marked not insertable; long text is truncated; a batch keeps only the newest version of each event.
- **Upsert (real Postgres):** inserts with history; a repeated batch changes nothing; newer overwrites; older never does but is kept in history; a stripped deletion hides an existing event; an unknown deletion is skipped; totals add up across batches.
- **API (real Postgres):** newest-first listing that hides deleted events and internal fields; every filter; seven kinds of invalid input return `400` in the standard shape; cursor pagination neither repeats nor skips when rows arrive between pages; the list is one query regardless of page size; detail by `usgs_id`; `404` and `410` in the standard shape, with history still readable for deleted events; expanded revisions newest first in two queries; the API is read-only; anonymous throttling returns `429` with a retry hint; operations endpoints reject anonymous clients; a backfill request creates windows and queues them only after commit; backfill input is validated; the status endpoint summarises both jobs; `/healthz` reports each dependency and returns `503` when one is down.
- **Workflows (real Postgres, fake client):** a window stores events, rejections and counts; a finished window is skipped without calling USGS; an oversized window splits into exact halves; permanent errors fail the window; transient errors return it to pending; creating windows twice is idempotent; the sync asks for `watermark − overlap` with deletions included; the watermark moves to the newest saved `updated`, never backwards, and not at all on failure; the sync splits its range when too many events changed; bootstrap is safe on every start and sets the watermark to the backfill start; the window task retries five times, then marks the window failed.

The client tests use `respx` with an injected `sleep`, so retry tests run instantly with no network.

---

## Known limitations

- **History only covers versions we fetched.** USGS publishes only the current version of an event; two revisions between syncs collapse into one, and a backfill sees only the latest version.
- **Bounding boxes don't cross the antimeridian** (e.g. a box spanning 170° to −170°); the API rejects them with a clear message. PostGIS would handle this properly.
- **Throttling depends on Redis.** If Redis is down, throttled requests fail. Acceptable here because Redis is already required for Celery.
- **A window left `running` by a hard-killed worker** relies on Celery re-delivery (`acks_late`) to be picked up again; there is no separate sweeper for stale `running` rows yet.
- **The sync watches events from `INGEST_HISTORY_START` onwards.** A revision to an older event, outside that range, is ignored. A revision inside it to an event we never backfilled is inserted as a new row.
- **A whole window is written in one transaction** (batches inside are savepoints). Fine for one-day windows; very dense windows are split first anyway.
- **The behaviour of USGS's default `starttime`** is assumed (30 days) and should be confirmed against the docs.
