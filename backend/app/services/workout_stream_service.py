"""Per-sample stream of ONE workout, as regular columns.

``GET /api/v1/users/{user_id}/events/workouts/{workout_id}/stream`` answers with
the samples recorded during a workout (heart rate, power, speed, cadence,
elevation, GPS, air temperature) laid out on a regular time grid, so a client
can draw a workout analysis (curves, zones, map, mean-max) without knowing which
provider recorded it.

Where the samples come from, in order:

1. **Stored samples** - ``data_point_series`` rows of the workout's user and
   provider that fall inside the workout window. This covers every integration
   that already writes per-sample data (FIT files parsed by ``fit_parser``,
   Apple HealthKit / Health Connect SDK heart rate, ...).
2. **Polar on demand** - Polar AccessLink does not push samples; they are read
   from ``GET /v3/exercises/{id}?samples=true&route=true`` when asked for.

Nothing is interpolated: a column only carries values that were measured, and a
grid slot with no measurement is ``None``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from statistics import median
from typing import Any
from uuid import UUID

import isodate
from sqlalchemy import select

from app.database import DbSession
from app.models import DataPointSeries, DataSource, EventRecord
from app.schemas.enums import ProviderName, SeriesType
from app.schemas.enums.series_types import get_series_type_id

logger = logging.getLogger(__name__)

#: Output columns, in the order they are emitted.
STREAM_COLUMNS: tuple[str, ...] = (
    "heart_rate",
    "power",
    "speed",
    "cadence",
    "elevation",
    "latitude",
    "longitude",
    "distance",
    "temperature",
)

#: Stored series -> output column. The first series of a column that has data wins.
_SERIES_FOR_COLUMN: dict[str, tuple[SeriesType, ...]] = {
    "heart_rate": (SeriesType.heart_rate,),
    "power": (SeriesType.power, SeriesType.running_power),
    "speed": (SeriesType.speed, SeriesType.running_speed),
    "cadence": (SeriesType.cadence,),
    "elevation": (SeriesType.elevation,),
    "latitude": (SeriesType.latitude,),
    "longitude": (SeriesType.longitude,),
    "temperature": (SeriesType.air_temperature,),
}

#: Polar AccessLink ``sample-type`` codes -> output column (value transform).
#: https://www.polar.com/accesslink-api/#exercise-samples (0 HR bpm, 1 speed km/h,
#: 2 cadence rpm, 3 altitude m, 4 power W, 9 temperature C, 10 distance m).
_POLAR_SAMPLE_TYPES: dict[str, tuple[str, float]] = {
    "0": ("heart_rate", 1.0),
    "1": ("speed", 1 / 3.6),
    "2": ("cadence", 1.0),
    "3": ("elevation", 1.0),
    "4": ("power", 1.0),
    "9": ("temperature", 1.0),
    "10": ("distance", 1.0),
}

#: Hard cap on grid length (24 h at 1 s), to keep one response bounded.
MAX_SAMPLES = 86_400


def _empty_columns(n: int) -> dict[str, list[float | None]]:
    return {c: [None] * n for c in STREAM_COLUMNS}


def _finalize(
    rate: float,
    n: int,
    columns: dict[str, list[float | None]],
) -> dict[str, Any] | None:
    """Drop columns without a single value; ``None`` when nothing was measured."""
    kept = {c: v for c, v in columns.items() if any(x is not None for x in v)}
    if not kept:
        return None
    out: dict[str, Any] = {"sample_rate_sec": rate, "t": [round(i * rate, 3) for i in range(n)]}
    for c in STREAM_COLUMNS:
        out[c] = kept.get(c)
    return out


def _grid(duration_sec: float, rate: float) -> int:
    if duration_sec <= 0 or rate <= 0:
        return 0
    return min(int(duration_sec // rate) + 1, MAX_SAMPLES)


def _num(raw: str) -> float | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        v = float(raw)
    except ValueError:
        return None
    return v if v == v else None  # NaN -> None


def polar_exercise_to_stream(exercise: dict[str, Any], duration_sec: float) -> dict[str, Any] | None:
    """Polar AccessLink exercise (``samples=true&route=true``) -> regular columns.

    Each sample type has its own ``recording-rate`` (s); the grid uses the
    smallest one, and a slower type fills only the slots it actually measured.
    Route points carry their own time (ISO duration from the start, or a
    timestamp); they go to the nearest grid slot.
    """
    samples = [s for s in (exercise.get("samples") or []) if isinstance(s, dict)]
    rates = [
        int(s.get("recording-rate") or s.get("recording_rate") or 0)
        for s in samples
        if str(s.get("sample-type", s.get("sample_type"))) in _POLAR_SAMPLE_TYPES
    ]
    rates = [r for r in rates if r > 0]
    route = [p for p in (exercise.get("route") or []) if isinstance(p, dict)]
    rate = float(min(rates)) if rates else 1.0
    n = _grid(duration_sec, rate)
    if n == 0:
        return None
    columns = _empty_columns(n)

    for s in samples:
        kind = str(s.get("sample-type", s.get("sample_type")))
        mapping = _POLAR_SAMPLE_TYPES.get(kind)
        own_rate = int(s.get("recording-rate") or s.get("recording_rate") or 0)
        if mapping is None or own_rate <= 0:
            continue
        column, factor = mapping
        values = str(s.get("data") or "").split(",")
        step = own_rate / rate
        for k, raw in enumerate(values):
            i = int(round(k * step))
            if i >= n:
                break
            v = _num(raw)
            if v is not None:
                columns[column][i] = round(v * factor, 4)

    first_ts: datetime | None = None
    for p in route:
        lat, lng = p.get("latitude"), p.get("longitude")
        if lat is None or lng is None:
            continue
        offset = _route_offset(p.get("time"))
        if offset is None:
            ts = _parse_ts(p.get("time"))
            if ts is None:
                continue
            first_ts = first_ts or ts
            offset = (ts - first_ts).total_seconds()
        i = int(round(offset / rate))
        if 0 <= i < n:
            columns["latitude"][i] = float(lat)
            columns["longitude"][i] = float(lng)

    return _finalize(rate, n, columns)


def _route_offset(raw: Any) -> float | None:
    """Seconds from the start when the point's time is an ISO duration (``PT1M2S``)."""
    if not isinstance(raw, str) or not raw.startswith("P"):
        return None
    try:
        return isodate.parse_duration(raw).total_seconds()
    except (isodate.ISO8601Error, ValueError):
        return None


def _parse_ts(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        ts = isodate.parse_datetime(raw)
    except (isodate.ISO8601Error, ValueError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def series_to_stream(
    points: list[tuple[datetime, str, float]],
    start: datetime,
    end: datetime,
) -> dict[str, Any] | None:
    """Stored samples ``(recorded_at, column, value)`` -> regular columns.

    The grid step is the median gap between samples of the densest column
    (at least 1 s): 1 s for a FIT file, ~5 s for a phone's heart rate. A sample
    goes to the slot of its own second; slots nobody measured stay ``None``.
    """
    if not points:
        return None
    start = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
    end = end if end.tzinfo else end.replace(tzinfo=timezone.utc)
    by_column: dict[str, list[tuple[float, float]]] = {}
    for at, column, value in points:
        at = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
        by_column.setdefault(column, []).append(((at - start).total_seconds(), value))
    densest = max(by_column.values(), key=len)
    offsets = sorted(o for o, _ in densest)
    gaps = [b - a for a, b in zip(offsets, offsets[1:]) if b > a]
    rate = float(max(1, int(median(gaps)))) if gaps else 1.0
    n = _grid((end - start).total_seconds(), rate)
    if n == 0:
        return None
    columns = _empty_columns(n)
    for column, values in by_column.items():
        for offset, value in values:
            i = int(round(offset / rate))
            if 0 <= i < n:
                columns[column][i] = value
    return _finalize(rate, n, columns)


class WorkoutStreamService:
    def get_stream(self, db: DbSession, user_id: UUID, workout_id: UUID) -> dict[str, Any] | None:
        """The workout's stream, or ``None`` when it is not the user's or has no samples."""
        record = db.get(EventRecord, workout_id)
        if record is None or record.category != "workout":
            return None
        data_source = db.get(DataSource, record.data_source_id)
        if data_source is None or data_source.user_id != user_id:
            return None
        provider = data_source.provider
        provider_key = provider.value if isinstance(provider, ProviderName) else str(provider)

        stream = self._from_stored_series(db, data_source, record)
        if stream is None and provider_key == ProviderName.POLAR.value and record.external_id:
            stream = self._from_polar(db, user_id, record)
        if stream is None:
            return None
        return {
            "workout_id": str(record.id),
            "provider": provider_key,
            "start_time": record.start_datetime.isoformat(),
            **stream,
        }

    def _from_stored_series(self, db: DbSession, data_source: DataSource, record: EventRecord) -> dict[str, Any] | None:
        column_of: dict[int, tuple[str, int]] = {}
        for column, series in _SERIES_FOR_COLUMN.items():
            for rank, s in enumerate(series):
                column_of[get_series_type_id(s)] = (column, rank)
        sources = select(DataSource.id).where(
            DataSource.user_id == data_source.user_id,
            DataSource.provider == data_source.provider,
        )
        rows = db.execute(
            select(DataPointSeries.recorded_at, DataPointSeries.series_type_definition_id, DataPointSeries.value)
            .where(
                DataPointSeries.data_source_id.in_(sources),
                DataPointSeries.series_type_definition_id.in_(list(column_of)),
                DataPointSeries.recorded_at >= record.start_datetime,
                DataPointSeries.recorded_at <= record.end_datetime,
            )
            .order_by(DataPointSeries.recorded_at)
        ).all()
        # One series per column: the preferred one that has data (power before running_power).
        best_rank: dict[str, int] = {}
        for _, type_id, _ in rows:
            column, rank = column_of[type_id]
            best_rank[column] = min(rank, best_rank.get(column, rank))
        points = [
            (at, column_of[type_id][0], float(value))
            for at, type_id, value in rows
            if column_of[type_id][1] == best_rank[column_of[type_id][0]] and value is not None
        ]
        return series_to_stream(points, record.start_datetime, record.end_datetime)

    def _from_polar(self, db: DbSession, user_id: UUID, record: EventRecord) -> dict[str, Any] | None:
        from app.services.providers.factory import ProviderFactory

        workouts = ProviderFactory().get_provider(ProviderName.POLAR.value).workouts
        if workouts is None:
            return None
        try:
            exercise = workouts.get_workout_detail_from_api(
                db, user_id, str(record.external_id), samples=True, route=True
            )
        except Exception:  # noqa: BLE001 - the provider being down is "no stream yet", not a 500
            logger.warning("Polar exercise %s: samples not available", record.external_id, exc_info=True)
            return None
        if not isinstance(exercise, dict):
            return None
        duration = (record.end_datetime - record.start_datetime).total_seconds()
        return polar_exercise_to_stream(exercise, duration)


workout_stream_service = WorkoutStreamService()

__all__ = [
    "STREAM_COLUMNS",
    "WorkoutStreamService",
    "polar_exercise_to_stream",
    "series_to_stream",
    "workout_stream_service",
]
