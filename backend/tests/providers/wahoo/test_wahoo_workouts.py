"""Wahoo workouts: normalisation of /v1/workouts items, paging and persistence."""

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

from sqlalchemy.orm import Session

from app.constants.workout_types.wahoo import get_activity_name, get_unified_workout_type
from app.models import EventRecord
from app.schemas.enums import WorkoutType
from app.schemas.providers.wahoo import WahooWorkoutJSON
from app.services.providers.wahoo.strategy import WahooStrategy
from tests.factories import UserConnectionFactory, UserFactory
from tests.providers.wahoo.conftest import make_summary, make_workout


class TestWahooWorkoutTypes:
    def test_known_ids(self) -> None:
        assert get_unified_workout_type(0) == WorkoutType.CYCLING
        assert get_unified_workout_type(1) == WorkoutType.RUNNING
        assert get_unified_workout_type(61) == WorkoutType.INDOOR_CYCLING
        assert get_unified_workout_type(25) == WorkoutType.POOL_SWIMMING
        assert get_activity_name(15) == "BIKING_ROAD"

    def test_unknown_or_missing_ids_fall_back_to_other(self) -> None:
        assert get_unified_workout_type(24) == WorkoutType.OTHER
        assert get_unified_workout_type(None) == WorkoutType.OTHER
        assert get_activity_name(999) == "UNKNOWN"


class TestWahooNormalisation:
    def test_decimal_strings_are_mapped_to_metrics(self, wahoo_strategy: WahooStrategy) -> None:
        user_id = UserFactory.build().id
        workout = WahooWorkoutJSON.model_validate(make_workout())

        record, detail = wahoo_strategy.workouts._normalize_workout(workout, user_id)

        assert record.source == "wahoo"
        assert record.external_id == "56519"
        assert record.type == WorkoutType.CYCLING.value
        assert record.start_datetime == datetime(2026, 7, 12, 9, 0, tzinfo=timezone.utc)
        assert record.duration_seconds == 275
        assert (record.end_datetime - record.start_datetime).total_seconds() == 275
        assert record.zone_offset == "+02:00"  # Europe/Madrid in July (CEST)
        assert detail.record_id == record.id
        assert detail.heart_rate_avg == Decimal("100.00")
        assert detail.energy_burned == Decimal("1500.00")
        assert detail.distance == Decimal("24909.71")
        assert detail.average_speed == Decimal("10.75")
        assert detail.average_watts == Decimal("94.59")
        assert detail.average_cadence == Decimal("50.00")
        assert detail.total_elevation_gain == Decimal("450.00")
        assert detail.moving_time_seconds == 179

    def test_missing_total_duration_falls_back_to_minutes(self, wahoo_strategy: WahooStrategy) -> None:
        workout = WahooWorkoutJSON.model_validate(
            make_workout(workout_summary=make_summary(duration_total_accum=None, time_zone="Not/AZone"))
        )
        record, _ = wahoo_strategy.workouts._normalize_workout(workout, UserFactory.build().id)
        assert record.duration_seconds == 300
        assert record.zone_offset is None


class TestWahooPaging:
    def test_stops_at_first_workout_older_than_start(self, wahoo_strategy: WahooStrategy) -> None:
        workouts = wahoo_strategy.workouts
        page_1 = {
            "workouts": [
                make_workout(3, "2026-07-20T08:00:00.000Z"),  # after end → excluded
                make_workout(2, "2026-07-10T08:00:00.000Z"),
                make_workout(1, "2026-06-01T08:00:00.000Z"),  # before start → stop
            ],
            "total": 3,
            "page": 1,
            "per_page": 3,
        }
        with (
            patch.object(type(workouts), "events_per_page", new=3),
            patch.object(workouts, "_make_api_request", return_value=page_1) as api,
        ):
            result = workouts.get_workouts(
                None,  # type: ignore[arg-type]
                UserFactory.build().id,
                datetime(2026, 7, 1, tzinfo=timezone.utc),
                datetime(2026, 7, 15, tzinfo=timezone.utc),
            )

        assert [w.id for w in result] == [2]
        api.assert_called_once()
        assert api.call_args.kwargs["params"] == {"page": 1, "per_page": 3}

    def test_reads_next_page_when_page_is_full(self, wahoo_strategy: WahooStrategy) -> None:
        workouts = wahoo_strategy.workouts
        pages = [
            {"workouts": [make_workout(2, "2026-07-10T08:00:00.000Z")]},
            {"workouts": [make_workout(1, "2026-07-05T08:00:00.000Z")]},
            {"workouts": []},
        ]
        with (
            patch.object(type(workouts), "events_per_page", new=1),
            patch.object(workouts, "_make_api_request", side_effect=pages) as api,
        ):
            result = workouts.get_workouts(
                None,  # type: ignore[arg-type]
                UserFactory.build().id,
                datetime(2026, 7, 1, tzinfo=timezone.utc),
                datetime(2026, 7, 15, tzinfo=timezone.utc),
            )
        assert [w.id for w in result] == [2, 1]
        assert api.call_count == 3


class TestWahooLoadData:
    def test_saves_completed_and_skips_planned_workouts(self, db: Session, wahoo_strategy: WahooStrategy) -> None:
        user = UserFactory()
        UserConnectionFactory(user=user, provider="wahoo", provider_user_id="60462")
        listing = {
            "workouts": [
                make_workout(10, "2026-07-12T09:00:00.000Z"),
                # Planned workout pushed by OW: no summary yet → must not be stored.
                make_workout(11, "2026-07-11T09:00:00.000Z", workout_summary=None, plan_id=77),
            ]
        }
        with patch.object(wahoo_strategy.workouts, "_make_api_request", return_value=listing):
            count = wahoo_strategy.workouts.load_data(
                db, user.id, start_date="2026-07-01T00:00:00Z", end_date="2026-07-15T00:00:00Z"
            )

        assert count == 1
        stored = db.query(EventRecord).filter(EventRecord.external_id.in_(["10", "11"])).all()
        assert [r.external_id for r in stored] == ["10"]

    def test_reloading_the_same_workout_does_not_duplicate(self, db: Session, wahoo_strategy: WahooStrategy) -> None:
        user = UserFactory()
        UserConnectionFactory(user=user, provider="wahoo")
        listing = {"workouts": [make_workout(20, "2026-07-12T09:00:00.000Z")]}
        with patch.object(wahoo_strategy.workouts, "_make_api_request", return_value=listing):
            for _ in range(2):
                wahoo_strategy.workouts.load_data(
                    db, user.id, start_date="2026-07-01T00:00:00Z", end_date="2026-07-15T00:00:00Z"
                )
        assert db.query(EventRecord).filter(EventRecord.external_id == "20").count() == 1
