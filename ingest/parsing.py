"""
Policy for bad data:
  - Missing *optional* values (mag, depth, sig, place) are kept as empty;
    fresh automatic solutions legitimately lack them.
  - *Impossible* values (latitude 91, magnitude 14, NaN) reject the record:
    they mean the payload is corrupt, and guessing would serve wrong data.
  - Deleted events only need an id and an `updated` time: USGS may strip
    the rest, and all we have to do is hide the event.
"""

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

VALID_STATUSES = {"automatic", "reviewed", "deleted"}

# Plausibility bounds. Generous on purpose: reject the impossible, not the unusual.
LAT_RANGE = (-90.0, 90.0)
LON_RANGE = (-180.0, 180.0)
MAG_RANGE = (-2.0, 10.0)
DEPTH_RANGE_KM = (-10.0, 1000.0)
TIME_RANGE = (datetime(1900, 1, 1, tzinfo=UTC), datetime(2100, 1, 1, tzinfo=UTC))

MAX_ID, MAX_MAG_TYPE, MAX_PLACE, MAX_TYPE = 32, 16, 255, 32


class InvalidFeature(ValueError):
    """Raised inside the parser; converted to a Rejection at the boundary."""


@dataclass(frozen=True)
class ParsedEvent:
    usgs_id: str
    time: datetime | None  # None only for deletions that arrive stripped
    updated: datetime
    status: str
    is_deleted: bool
    mag: float | None = None
    mag_type: str = ""
    latitude: float | None = None
    longitude: float | None = None
    depth_km: float | None = None
    place: str = ""
    event_type: str = "earthquake"
    tsunami: bool = False
    sig: int | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def is_complete(self) -> bool:
        return (
            self.time is not None
            and self.latitude is not None
            and self.longitude is not None
        )


@dataclass(frozen=True)
class Rejection:
    usgs_id: str
    reason: str
    payload: Any


def parse_feature(feature: Any) -> ParsedEvent | Rejection:
    usgs_id = _peek_id(feature)
    try:
        return _parse(feature)
    except InvalidFeature as exc:
        return Rejection(usgs_id=usgs_id, reason=str(exc), payload=feature)


def parse_features(features: list[Any]) -> tuple[list[ParsedEvent], list[Rejection]]:
    """
    Parse a batch. Also de-duplicates by usgs_id, keeping the newest version.
    """
    newest: dict[str, ParsedEvent] = {}
    rejections: list[Rejection] = []

    for feature in features:
        result = parse_feature(feature)
        if isinstance(result, Rejection):
            rejections.append(result)
            continue
        current = newest.get(result.usgs_id)
        if current is None or result.updated > current.updated:
            newest[result.usgs_id] = result

    return list(newest.values()), rejections


def _parse(feature: Any) -> ParsedEvent:
    if not isinstance(feature, dict):
        raise InvalidFeature("feature is not an object")

    usgs_id = feature.get("id")
    if not isinstance(usgs_id, str) or not usgs_id.strip():
        raise InvalidFeature("missing id")
    usgs_id = usgs_id.strip()
    if len(usgs_id) > MAX_ID:
        raise InvalidFeature(f"id longer than {MAX_ID} characters")

    props = feature.get("properties")
    if not isinstance(props, dict):
        raise InvalidFeature("missing properties")

    status = props.get("status")
    status = status.strip().lower() if isinstance(status, str) else None
    if status not in VALID_STATUSES:
        raise InvalidFeature(f"unknown status: {props.get('status')!r}")

    updated = _epoch_ms(props.get("updated"), "updated", required=True)
    is_deleted = status == "deleted"

    time = _epoch_ms(props.get("time"), "time", required=not is_deleted)
    lon, lat, depth = _coordinates(feature.get("geometry"), required=not is_deleted)

    return ParsedEvent(
        usgs_id=usgs_id,
        time=time,
        updated=updated,
        status=status,
        is_deleted=is_deleted,
        mag=_number(props.get("mag"), "mag", MAG_RANGE),
        mag_type=_text(props.get("magType"), MAX_MAG_TYPE).lower(),
        latitude=lat,
        longitude=lon,
        depth_km=depth,
        place=_text(props.get("place"), MAX_PLACE),
        event_type=_text(props.get("type"), MAX_TYPE).lower() or "earthquake",
        tsunami=props.get("tsunami") in (1, True, "1"),
        sig=_integer(props.get("sig"), "sig"),
        raw=feature,
    )


def _coordinates(
    geometry: Any, *, required: bool
) -> tuple[float | None, float | None, float | None]:
    coords = geometry.get("coordinates") if isinstance(geometry, dict) else None
    if coords is None and not required:
        return None, None, None
    if not isinstance(coords, list) or len(coords) < 2:
        raise InvalidFeature("geometry.coordinates must be [lon, lat, depth]")

    lon = _number(coords[0], "longitude", LON_RANGE, required=True)
    lat = _number(coords[1], "latitude", LAT_RANGE, required=True)
    depth = _number(coords[2], "depth", DEPTH_RANGE_KM) if len(coords) > 2 else None
    return lon, lat, depth


def _number(
    value: Any, name: str, bounds: tuple[float, float], *, required: bool = False
) -> float | None:
    if value is None:
        if required:
            raise InvalidFeature(f"missing {name}")
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidFeature(f"{name} is not a number: {value!r}")
    value = float(value)
    if not math.isfinite(value):
        raise InvalidFeature(f"{name} is not finite: {value!r}")
    low, high = bounds
    if not low <= value <= high:
        raise InvalidFeature(f"{name} out of range [{low}, {high}]: {value}")
    return value


def _integer(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise InvalidFeature(f"{name} is not a number: {value!r}")
    return int(value)


def _epoch_ms(value: Any, name: str, *, required: bool) -> datetime | None:
    """USGS times are integer milliseconds since the Unix epoch, UTC."""
    if value is None:
        if required:
            raise InvalidFeature(f"missing {name}")
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise InvalidFeature(f"{name} is not an epoch-milliseconds number: {value!r}")
    try:
        dt = datetime.fromtimestamp(value / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise InvalidFeature(f"{name} out of range: {value!r}") from exc
    low, high = TIME_RANGE
    if not low <= dt < high:
        raise InvalidFeature(f"{name} implausible: {dt.isoformat()}")
    return dt


def _text(value: Any, max_len: int) -> str:
    """Optional free text: None becomes "", too long is truncated (never rejected)."""
    if value is None:
        return ""
    return str(value).strip()[:max_len]


def _peek_id(feature: Any) -> str:
    value = feature.get("id") if isinstance(feature, dict) else None
    return value.strip()[:MAX_ID] if isinstance(value, str) else ""
