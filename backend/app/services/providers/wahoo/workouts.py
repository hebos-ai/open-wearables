"""Wahoo workouts: pull (``GET /v1/workouts``) and push (``workout_summary`` webhook).

Only *completed* workouts are stored: a Wahoo workout without ``workout_summary`` is a
planned/upcoming entry (e.g. one created by the planned-workout push) and is skipped.
"""

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import settings
from app.constants.workout_types import get_unified_wahoo_workout_type
from app.database import DbSession
from app.schemas.model_crud.activities import (
    EventRecordCreate,
    EventRecordDetailCreate,
    EventRecordMetrics,
)
from app.schemas.providers.wahoo import WahooWorkoutJSON, WahooWorkoutListJSON, WahooWorkoutSummaryJSON
from app.services.event_record_service import event_record_service
from app.services.providers.templates.base_workouts import BaseWorkoutsTemplate
from app.utils.dates import offset_to_iso
from app.utils.structured_logging import log_structured

# Hard stop for a single backfill, protects the (very low) sandbox rate limits.
MAX_PAGES = 100


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _parse_dt(value: Any, default: datetime) -> datetime:
    if isinstance(value, datetime):
        return _as_utc(value)
    if isinstance(value, str) and value:
        return _as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    return default


class WahooWorkouts(BaseWorkoutsTemplate):
    """Wahoo implementation of workouts template."""

    @property
    def events_per_page(self) -> int:
        return settings.wahoo_events_per_page

    # ------------------------------------------------------------------
    # Fetching
    # ------------------------------------------------------------------

    def get_workouts(
        self,
        db: DbSession,
        user_id: UUID,
        start_date: datetime,
        end_date: datetime,
    ) -> list[WahooWorkoutJSON]:
        """Page through ``/v1/workouts`` (newest first) until past ``start_date``.

        Wahoo has no date filter, so pages are read in ``starts`` descending order and
        filtering happens here.
        """
        start_utc, end_utc = _as_utc(start_date), _as_utc(end_date)
        collected: list[WahooWorkoutJSON] = []

        for page in range(1, MAX_PAGES + 1):
            response = self._make_api_request(
                db,
                user_id,
                "/v1/workouts",
                params={"page": page, "per_page": self.events_per_page},
            )
            listing = WahooWorkoutListJSON.model_validate(response or {})
            if not listing.workouts:
                break

            reached_older = False
            for workout in listing.workouts:
                starts = _as_utc(workout.starts)
                if starts < start_utc:
                    reached_older = True
                    continue
                if starts <= end_utc:
                    collected.append(workout)

            if reached_older or len(listing.workouts) < self.events_per_page:
                break
        else:
            log_structured(
                self.logger,
                "warning",
                "Wahoo backfill hit page limit",
                provider="wahoo",
                user_id=str(user_id),
                max_pages=MAX_PAGES,
            )

        return collected

    def get_workouts_from_api(self, db: DbSession, user_id: UUID, **kwargs: Any) -> Any:
        """Raw ``/v1/workouts`` page (vendor passthrough endpoint)."""
        page = kwargs.get("page") or 1
        return self._make_api_request(
            db, user_id, "/v1/workouts", params={"page": page, "per_page": self.events_per_page}
        )

    def get_workout_detail_from_api(self, db: DbSession, user_id: UUID, workout_id: str, **kwargs: Any) -> Any:
        return self._make_api_request(db, user_id, f"/v1/workouts/{workout_id}")

    # ------------------------------------------------------------------
    # Normalisation
    # ------------------------------------------------------------------

    @staticmethod
    def _zone_offset(time_zone: str | None, at: datetime) -> str | None:
        if not time_zone:
            return None
        try:
            offset = at.astimezone(ZoneInfo(time_zone)).utcoffset()
        except (ZoneInfoNotFoundError, ValueError):
            return None
        return offset_to_iso(int(offset.total_seconds())) if offset is not None else None

    @staticmethod
    def _build_metrics(summary: WahooWorkoutSummaryJSON) -> EventRecordMetrics:
        metrics: EventRecordMetrics = {}
        if summary.heart_rate_avg is not None:
            metrics["heart_rate_avg"] = summary.heart_rate_avg
        if summary.calories_accum is not None:
            metrics["energy_burned"] = summary.calories_accum
        if summary.distance_accum is not None:
            metrics["distance"] = summary.distance_accum
        if summary.speed_avg is not None:
            metrics["average_speed"] = summary.speed_avg
        if summary.power_avg is not None:
            metrics["average_watts"] = summary.power_avg
        if summary.cadence_avg is not None:
            metrics["average_cadence"] = summary.cadence_avg
        if summary.ascent_accum is not None:
            metrics["total_elevation_gain"] = summary.ascent_accum
        if summary.duration_active_accum is not None:
            metrics["moving_time_seconds"] = int(summary.duration_active_accum)
        return metrics

    def _normalize_workout(
        self,
        raw_workout: WahooWorkoutJSON,
        user_id: UUID,
    ) -> tuple[EventRecordCreate, EventRecordDetailCreate]:
        """Normalise a *completed* Wahoo workout (caller guarantees ``workout_summary``)."""
        summary = raw_workout.workout_summary or WahooWorkoutSummaryJSON()
        record_id = uuid4()

        start = _as_utc(raw_workout.starts)
        if summary.duration_total_accum is not None:
            duration_seconds = int(summary.duration_total_accum)
        elif raw_workout.minutes is not None:
            duration_seconds = raw_workout.minutes * 60
        else:
            duration_seconds = 0
        end = start + timedelta(seconds=duration_seconds)

        record = EventRecordCreate(
            category="workout",
            type=get_unified_wahoo_workout_type(raw_workout.workout_type_id).value,
            source_name="Wahoo",
            device_model=None,
            duration_seconds=duration_seconds,
            start_datetime=start,
            end_datetime=end,
            zone_offset=self._zone_offset(summary.time_zone, start),
            id=record_id,
            external_id=str(raw_workout.id),
            source="wahoo",
            user_id=user_id,
        )
        detail = EventRecordDetailCreate(record_id=record_id, **self._build_metrics(summary))
        return record, detail

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save_workout(self, db: DbSession, user_id: UUID, workout: WahooWorkoutJSON) -> UUID | None:
        """Persist one completed workout. Returns the record id, or None if it is not completed."""
        if workout.workout_summary is None:
            return None
        record, detail = self._normalize_workout(workout, user_id)
        created = event_record_service.create(db, record)
        if created.id != record.id:
            # Already stored (overlapping pull window or a Wahoo webhook retry): keep it idempotent.
            return created.id
        event_record_service.create_detail(db, detail.model_copy(update={"record_id": created.id}))
        return created.id

    def load_data(self, db: DbSession, user_id: UUID, **kwargs: Any) -> int:
        """Pull completed workouts in [start_date, end_date] (defaults: last 30 days)."""
        now = datetime.now(timezone.utc)
        start = _parse_dt(kwargs.get("start_date") or kwargs.get("start"), now - timedelta(days=30))
        end = _parse_dt(kwargs.get("end_date") or kwargs.get("end"), now)

        count = 0
        for workout in self.get_workouts(db, user_id, start, end):
            if self.save_workout(db, user_id, workout) is not None:
                count += 1
        return count
