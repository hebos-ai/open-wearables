"""Oura daily stress, daily resilience, sessions and rest mode."""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.models import DataPointSeries, EventRecord, HealthScore
from app.schemas.enums import HealthScoreCategory, ProviderName, SeriesType
from app.schemas.enums.series_types import get_series_type_id
from app.services.providers.oura.coverage import HEALTH_SCORES, READINESS_SERIES, TIMESERIES
from app.services.providers.oura.data_247 import Oura247Data
from app.services.providers.oura.strategy import OuraStrategy
from app.services.providers.oura.webhook_handler import SUPPORTED_DATA_TYPES, OuraWebhookHandler
from app.services.providers.oura.webhook_service import OURA_WEBHOOK_DATA_TYPES
from tests.factories import UserFactory

STRESS = {
    "id": "stress-1",
    "day": "2026-10-20",
    "stress_high": 5400,
    "recovery_high": 1800,
    "day_summary": "stressful",
}
RESILIENCE = {
    "id": "res-1",
    "day": "2026-10-20",
    "level": "solid",
    "contributors": {"sleep_recovery": 72.5, "daytime_recovery": 40.0, "stress": 55.0},
}
SESSION = {
    "id": "sess-1",
    "day": "2026-10-20",
    "start_datetime": "2026-10-20T19:00:00+02:00",
    "end_datetime": "2026-10-20T19:10:00+02:00",
    "type": "breathing",
    "mood": "good",
    "heart_rate": {"interval": 5, "items": [60, None, 58, 62], "timestamp": "2026-10-20T19:00:00+02:00"},
    "heart_rate_variability": {"interval": 5, "items": [45.0, 50.0, None], "timestamp": "2026-10-20T19:00:00+02:00"},
    "motion_count": {"interval": 5, "items": [0, 1], "timestamp": "2026-10-20T19:00:00+02:00"},
}
REST_MODE = {
    "id": "rest-1",
    "start_day": "2026-10-18",
    "end_day": "2026-10-20",
    "start_time": "2026-10-18T08:00:00+02:00",
    "end_time": "2026-10-20T09:30:00+02:00",
    "episodes": [{"tags": ["tag_generic_sick"], "timestamp": "2026-10-18T08:00:00+02:00"}],
}


@pytest.fixture
def data_247() -> Oura247Data:
    return OuraStrategy().data_247


class TestNormalization:
    def test_daily_stress_is_a_stress_score_without_value(self, data_247: Oura247Data) -> None:
        user_id = uuid4()
        [score] = data_247.normalize_daily_stress([STRESS], user_id)

        assert score.category == HealthScoreCategory.STRESS
        assert score.provider == ProviderName.OURA
        assert score.value is None
        assert score.qualifier == "stressful"
        assert score.recorded_at == datetime(2026, 10, 20, tzinfo=timezone.utc)
        assert score.components is not None
        assert score.components["stress_high_minutes"].value == 90.0
        assert score.components["recovery_high_minutes"].value == 30.0

    def test_daily_stress_empty_document_is_skipped(self, data_247: Oura247Data) -> None:
        assert data_247.normalize_daily_stress([{"id": "x", "day": "2026-10-20"}], uuid4()) == []

    def test_daily_resilience_level_and_contributors(self, data_247: Oura247Data) -> None:
        [score] = data_247.normalize_daily_resilience([RESILIENCE], uuid4())

        assert score.category == HealthScoreCategory.RESILIENCE
        assert score.qualifier == "solid"
        assert score.value is None
        assert score.components is not None
        assert {k: c.value for k, c in score.components.items()} == {
            "sleep_recovery": 72.5,
            "daytime_recovery": 40.0,
            "stress": 55.0,
        }

    def test_session_becomes_meditation_workout_with_hrv(self, data_247: Oura247Data) -> None:
        [(record, detail, samples)] = data_247.normalize_sessions([SESSION], uuid4())

        assert record.category == "workout"
        assert record.type == "meditation"
        assert record.external_id == "sess-1"
        assert record.duration_seconds == 600
        assert record.zone_offset == "+02:00"
        assert detail.heart_rate_min == Decimal("58")
        assert detail.heart_rate_max == Decimal("62")
        assert detail.heart_rate_avg == Decimal("60.00")
        assert [s.series_type for s in samples] == [SeriesType.heart_rate_variability_rmssd] * 2
        assert [s.value for s in samples] == [Decimal("45.0"), Decimal("50.0")]
        assert samples[1].recorded_at - samples[0].recorded_at == (datetime(2026, 1, 1, 0, 0, 5) - datetime(2026, 1, 1))

    def test_nap_session_is_skipped(self, data_247: Oura247Data) -> None:
        assert data_247.normalize_sessions([{**SESSION, "type": "nap"}], uuid4()) == []

    def test_rest_mode_finished_period(self, data_247: Oura247Data) -> None:
        [record] = data_247.normalize_rest_mode_periods([REST_MODE], uuid4())

        assert record.category == "rest_mode"
        assert record.external_id == "rest-1"
        assert record.duration_seconds == 49 * 3600 + 1800

    def test_rest_mode_ongoing_period_is_not_stored(self, data_247: Oura247Data) -> None:
        ongoing = {**REST_MODE, "end_day": None, "end_time": None}
        assert data_247.normalize_rest_mode_periods([ongoing], uuid4()) == []

    def test_rest_mode_with_only_days(self, data_247: Oura247Data) -> None:
        only_days = {**REST_MODE, "start_time": None, "end_time": None}
        [record] = data_247.normalize_rest_mode_periods([only_days], uuid4())
        assert record.start_datetime == datetime(2026, 10, 18, tzinfo=timezone.utc)
        assert record.end_datetime == datetime(2026, 10, 21, tzinfo=timezone.utc)


class TestTemperatureIsADeviation:
    def test_readiness_temperatures_map_to_deviation_series(self) -> None:
        assert {
            "temperature_deviation": SeriesType.skin_temperature_deviation,
            "temperature_trend_deviation": SeriesType.skin_temperature_trend_deviation,
        } == READINESS_SERIES
        assert SeriesType.body_temperature not in TIMESERIES
        assert SeriesType.skin_temperature not in TIMESERIES

    def test_readiness_deviation_values_are_kept_as_is(self, data_247: Oura247Data) -> None:
        metrics, _ = data_247.normalize_readiness(
            [{"id": "r", "day": "2026-10-20", "score": 80, "temperature_deviation": -0.21}], uuid4()
        )
        assert metrics[0]["temperature_deviation"] == -0.21


class TestCoverageAndWiring:
    def test_health_scores_coverage(self) -> None:
        assert {HealthScoreCategory.STRESS, HealthScoreCategory.RESILIENCE} <= HEALTH_SCORES
        assert SeriesType.heart_rate_variability_rmssd in TIMESERIES

    @pytest.mark.parametrize("data_type", ["daily_stress", "daily_resilience", "session", "rest_mode_period"])
    def test_webhook_data_types_registered_and_handled(self, data_type: str) -> None:
        assert data_type in OURA_WEBHOOK_DATA_TYPES
        assert data_type in SUPPORTED_DATA_TYPES

    def test_default_scope_has_no_invalid_activity_scope(self) -> None:
        from app.config import settings

        scopes = settings.oura_default_scope.split()
        assert "activity" not in scopes
        assert {"daily", "session"} <= set(scopes)


class TestPersistence:
    def test_save_stress_and_resilience_scores(self, db: Session, data_247: Oura247Data) -> None:
        user = UserFactory()
        saved = data_247.save_health_scores(db, data_247.normalize_daily_stress([STRESS], user.id))
        saved += data_247.save_health_scores(db, data_247.normalize_daily_resilience([RESILIENCE], user.id))

        assert saved == 2
        rows = db.query(HealthScore).filter(HealthScore.user_id == user.id).all()
        by_cat = {r.category: r for r in rows}
        assert by_cat[HealthScoreCategory.STRESS].qualifier == "stressful"
        assert by_cat[HealthScoreCategory.RESILIENCE].qualifier == "solid"
        assert by_cat[HealthScoreCategory.RESILIENCE].components["sleep_recovery"]["value"] == 72.5

    def test_todays_stress_is_updated_during_the_day(self, db: Session, data_247: Oura247Data) -> None:
        """Oura re-sends daily_stress as the day goes on: the last version must win, not the first."""
        user = UserFactory()
        morning = {**STRESS, "stress_high": 600, "day_summary": None}
        data_247.save_health_scores(db, data_247.normalize_daily_stress([morning], user.id))
        data_247.save_health_scores(db, data_247.normalize_daily_stress([STRESS], user.id))

        (row,) = db.query(HealthScore).filter(HealthScore.user_id == user.id).all()
        assert row.qualifier == "stressful"
        assert row.components["stress_high_minutes"]["value"] == 90.0

    def test_save_session_and_rest_mode(self, db: Session, data_247: Oura247Data) -> None:
        user = UserFactory()
        assert data_247.save_sessions(db, user.id, data_247.normalize_sessions([SESSION], user.id)) == 1
        assert data_247.save_rest_mode_periods(db, data_247.normalize_rest_mode_periods([REST_MODE], user.id)) == 1

        categories = sorted(r.category for r in db.query(EventRecord).all() if r.external_id in {"sess-1", "rest-1"})
        assert categories == ["rest_mode", "workout"]
        hrv = (
            db.query(DataPointSeries)
            .filter(
                DataPointSeries.series_type_definition_id == get_series_type_id(SeriesType.heart_rate_variability_rmssd)
            )
            .count()
        )
        assert hrv == 2

    def test_load_and_save_all_calls_new_endpoints(self, db: Session, data_247: Oura247Data) -> None:
        user = UserFactory()
        responses: dict[str, list[dict[str, Any]]] = {
            "/v2/usercollection/daily_stress": [STRESS],
            "/v2/usercollection/daily_resilience": [RESILIENCE],
            "/v2/usercollection/session": [SESSION],
            "/v2/usercollection/rest_mode_period": [REST_MODE],
        }
        data_247._paginate = MagicMock(side_effect=lambda _db, _uid, endpoint, _p: responses.get(endpoint, []))  # type: ignore[method-assign]
        data_247._make_api_request = MagicMock(return_value={})  # type: ignore[method-assign]

        results = data_247.load_and_save_all(
            db, user.id, datetime(2026, 10, 18, tzinfo=timezone.utc), datetime(2026, 10, 21, tzinfo=timezone.utc)
        )

        assert results["daily_stress"] == 1
        assert results["daily_resilience"] == 1
        assert results["session"] == 1
        assert results["rest_mode"] == 1


class TestWebhookDispatch:
    @pytest.mark.parametrize(
        ("data_type", "doc", "method"),
        [
            ("daily_stress", STRESS, "save_health_scores"),
            ("daily_resilience", RESILIENCE, "save_health_scores"),
            ("session", SESSION, "save_sessions"),
            ("rest_mode_period", REST_MODE, "save_rest_mode_periods"),
        ],
    )
    def test_dispatch_routes_new_types(self, data_type: str, doc: dict, method: str) -> None:
        strategy = OuraStrategy()
        handler = OuraWebhookHandler(strategy.data_247, strategy.workouts)
        handler.data_247 = MagicMock(wraps=strategy.data_247)
        handler.data_247._make_api_request = MagicMock(return_value=doc)
        setattr(handler.data_247, method, MagicMock(return_value=1))
        notification = MagicMock(data_type=data_type, object_id=doc["id"], user_id="oura-user", event_type="create")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("app.services.providers.oura.webhook_handler.store_raw_payload", lambda **_: None)
            assert handler._dispatch_data_type(MagicMock(), notification, uuid4(), "trace") == 1
        getattr(handler.data_247, method).assert_called_once()
