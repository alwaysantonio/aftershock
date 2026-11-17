import random
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Callable

from config.exceptions import PermanentError, TransientError, WindowTooLarge

import httpx

DEFAULT_BASE_URL = "https://earthquake.usgs.gov/fdsnws/event/1"
MAX_EVENTS_PER_QUERY = 20_000
USER_AGENT = "aftershock/0.1 (+https://github.com/alwaysantonio/aftershock)"


@dataclass(frozen=True)
class EventQuery:
    """
    starttime is always sent explicitly: USGS appears to default it to 30 days
    ago, which would make an `updatedafter` sync silently skip revisions to
    older events.
    """

    starttime: datetime
    endtime: datetime | None = None
    updatedafter: datetime | None = None
    include_deleted: bool = False

    def params(self) -> dict[str, str]:
        p = {
            "format": "geojson",
            "orderby": "time-asc",  # deterministic order for repeatable batches
            "starttime": _iso(self.starttime),
        }
        if self.endtime is not None:
            p["endtime"] = _iso(self.endtime)
        if self.updatedafter is not None:
            p["updatedafter"] = _iso(self.updatedafter)
        if self.include_deleted:
            p["includedeleted"] = "true"
        return p


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("datetimes passed to the USGS client must be timezone-aware")
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")


class USGSClient:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        connect_timeout: float = 5.0,
        read_timeout: float = 30.0,
        max_attempts: int = 3,
        backoff_base: float = 0.5,
        backoff_cap: float = 8.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        """
        I chose httpx mainly for its default timeouts.
        A forgotten timeout means a worker can hang forever and the same API in async if we ever need concurrency.
        Requests would have been fine too.
        """

        self._http = httpx.Client(
            base_url=base_url,
            timeout=httpx.Timeout(read_timeout, connect=connect_timeout),
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            transport=transport,
        )
        self._max_attempts = max_attempts
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._sleep = sleep  # injectable so tests don't actually wait

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> USGSClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def count(self, query: EventQuery) -> int:
        """How many events match. Cheap; call before fetching a window."""
        params = {k: v for k, v in query.params().items() if k != "orderby"}
        data = self._get_json("/count", params)
        try:
            return int(data["count"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PermanentError(f"unexpected /count response: {data!r}") from exc

    def fetch(self, query: EventQuery) -> list[dict[str, Any]]:
        """
        Two retry layers, deliberately. The client retries only brief blips, within seconds.
        Longer outages become a TransientError, which the Celery task retries later with a longer backoff.
        That keeps a worker from being held for minutes, and stops the two layers multiplying into a burst of requests.
        """
        n = self.count(query)
        if n > MAX_EVENTS_PER_QUERY:
            raise WindowTooLarge(n)
        if n == 0:
            return []

        data = self._get_json("/query", query.params())
        features = data.get("features") if isinstance(data, dict) else None
        if not isinstance(features, list):
            raise PermanentError("response has no 'features' list")
        return features

    def _get_json(self, path: str, params: dict[str, str]) -> Any:
        last_error: TransientError | None = None

        for attempt in range(1, self._max_attempts + 1):
            try:
                return self._get_once(path, params)
            except TransientError as exc:
                last_error = exc
                if attempt == self._max_attempts:
                    break
                self._sleep(self._delay(attempt, exc.retry_after))

        assert last_error is not None
        raise last_error

    def _get_once(self, path: str, params: dict[str, str]) -> Any:
        try:
            resp = self._http.get(path, params=params)
        except httpx.TimeoutException as exc:
            raise TransientError(f"timeout calling USGS {path}") from exc
        except httpx.TransportError as exc:  # DNS, connection refused/reset
            raise TransientError(f"network error calling USGS {path}: {exc}") from exc

        status = resp.status_code
        if status == 200:
            try:
                return resp.json()
            except ValueError as exc:
                raise PermanentError(f"USGS {path} returned invalid JSON") from exc
        if status == 204:  # some FDSN services answer "no data" this way
            return {"count": 0} if path == "/count" else {"features": []}
        if status == 429:
            raise TransientError("rate limited by USGS", _retry_after(resp))
        if status >= 500:
            raise TransientError(f"USGS {path} returned {status}", _retry_after(resp))
        # Other 4xx (400 bad request, 409 deleted event lookup...): retrying won't change it.
        raise PermanentError(f"USGS {path} returned {status}: {resp.text[:300]}")

    def _delay(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(retry_after, self._backoff_cap * 4)  # honour it, within reason
        # Exponential backoff with full jitter: 0..min(cap, base * 2^(n-1))
        return random.uniform(
            0, min(self._backoff_cap, self._backoff_base * 2 ** (attempt - 1))
        )


def _retry_after(resp: httpx.Response) -> float | None:
    """Parse Retry-After as seconds or an HTTP date."""
    value = resp.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
        return max(0.0, (when - datetime.now(UTC)).total_seconds())
    except TypeError, ValueError:
        return None
