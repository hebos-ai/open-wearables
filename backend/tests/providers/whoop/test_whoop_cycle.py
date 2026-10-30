"""Whoop /v2/cycle: daily strain (health_score) and energy (kJ -> kcal)."""

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.config import settings
from app.models import DataPointSeries, HealthScore
from app.schemas.enums import HealthScoreCategory, SeriesType, get_series_type_id
from app.services.providers.whoop.coverage import HEALTH_SCORES, TIMESERIES
from app.services.providers.whoop.data_247 import Whoop247Data, _cycle_day_start
from app.services.providers.whoop.strategy import WhoopStrategy
from app.services.providers.whoop.webhook_handler import WhoopWebhookNotificationType
from tests.factories import UserConnectionFactory, UserFactory


@pytest.fixture
def data_247() -> Whoop247Data:
    return WhoopStrategy().data_247  # ty:ignore[invalid-return-type]


def _cycle(**overrides: object) -> dict:
    cycle = {
        "id": 93845,
        "user_id": 10129,
        "start": "2026-10-28T22:30:00.000Z",
        "end": "2026-10-29T22:10:00.000Z",
        "timezone_offset": "+01:00",
        "score_state": "SCORED",
        "score": {
            "strain": 14.2,
            "kilojoule": 10460.0,
            "average_heart_rate": 71,
            "max_heart_rate": 172,
        },
    }
    cycle.update(overrides)
    return cycle


class TestCycleDayAnchor:
    def test_bedtime_before_midnight_lands_on_the_next_local_day(self) -> None:
        # Starts 23:30 local on the 28th, ends 23:10 local on the 29th -> the 29th.
        start = datetime(2026, 10, 28, 22, 30, tzinfo=timezone.utc)
        end = datetime(2026, 10, 29, 22, 10, tzinfo=timezone.utc)
        assert _cycle_day_start(start, end, "+01:00") == datetime(2026, 10, 28, 23, 0, tzinfo=timezone.utc)

    def test_utc_and_unparseable_offsets(self) -> None:
        start = datetime(2026, 10, 29, 1, 0, tzinfo=timezone.utc)
        end = datetime(2026, 10, 30, 0, 30, tzinfo=timezone.utc)
        expected = datetime(2026, 10, 29, 0, 0, tzinfo=timezone.utc)
        assert _cycle_day_start(start, end, "Z") == expected
        assert _cycle_day_start(start, end, None) == expected
        assert _cycle_day_start(start, end, "garbage") == expected

    def test_negative_offset(self) -> None:
        # 22:00 local (UTC-5) on the 28th to 21:00 local on the 29th -> the 29th local.
        start = datetime(2026, 10, 29, 3, 0, tzinfo=timezone.utc)
        end = datetime(2026, 10, 30, 2, 0, tzinfo=timezone.utc)
        assert _cycle_day_start(start, end, "-05:00") == datetime(2026, 10, 29, 5, 0, tzinfo=timezone.utc)


class TestNormalizeCycle:
    def test_energy_is_kcal_daily_total(self, data_247: Whoop247Data) -> None:
        user_id = uuid4()
        samples, _ = data_247.normalize_cycle(_cycle(), user_id)

        assert len(samples) == 1
        energy = samples[0]
        assert energy.series_type == SeriesType.energy
        # 10460 kJ * 0.239006 = 2500.00 kcal
        assert energy.value == Decimal("2500.00")
        assert energy.is_daily_total is True
        assert energy.zone_offset == "+01:00"
        assert energy.recorded_at == datetime(2026, 10, 28, 23, 0, tzinfo=timezone.utc)
        assert energy.external_id == "whoop-cycle-93845-energy"
        assert energy.user_id == user_id

    def test_strain_health_score(self, data_247: Whoop247Data) -> None:
        _, score = data_247.normalize_cycle(_cycle(), uuid4())

        assert score is not None
        assert score.category == HealthScoreCategory.STRAIN
        assert score.value == pytest.approx(14.2)
        assert score.recorded_at == datetime(2026, 10, 28, 23, 0, tzinfo=timezone.utc)
        assert score.zone_offset == "+01:00"
        assert score.components is not None
        assert score.components["average_heart_rate"].value == 71
        assert score.components["max_heart_rate"].value == 172
        assert score.components["kilojoule"].value == 10460.0

    @pytest.mark.parametrize(
        "overrides",
        [
            {"end": None},  # still running: strain keeps climbing
            {"score_state": "PENDING_SCORE"},
            {"score_state": "UNSCORABLE", "score": None},
            {"start": "not-a-date"},
        ],
    )
    def test_skips_unfinished_or_unscored_cycles(self, data_247: Whoop247Data, overrides: dict) -> None:
        assert data_247.normalize_cycle(_cycle(**overrides), uuid4()) == ([], None)

    def test_missing_kilojoule_keeps_strain_only(self, data_247: Whoop247Data) -> None:
        samples, score = data_247.normalize_cycle(
            _cycle(score={"strain": 8.0, "average_heart_rate": 60, "max_heart_rate": 120}), uuid4()
        )
        assert samples == []
        assert score is not None
        assert "kilojoule" not in (score.components or {})


class TestCycleFetch:
    def test_paginates_with_next_token(self, data_247: Whoop247Data) -> None:
        pages = [
            {"records": [_cycle(id=1)], "next_token": "abc"},
            {"records": [_cycle(id=2)], "next_token": None},
        ]
        with (
            patch.object(data_247, "_make_api_request", side_effect=pages) as req,
            patch("app.services.providers.whoop.data_247.store_raw_payload"),
        ):
            cycles = data_247.get_cycle_data(
                MagicMock(),
                uuid4(),
                datetime(2026, 10, 1, tzinfo=timezone.utc),
                datetime(2026, 10, 30, tzinfo=timezone.utc),
            )

        assert [c["id"] for c in cycles] == [1, 2]
        assert req.call_args_list[0].args[2] == "/v2/cycle"
        assert req.call_args_list[1].kwargs["params"]["nextToken"] == "abc"

    def test_partial_pages_are_kept(self, data_247: Whoop247Data) -> None:
        with (
            patch.object(
                data_247,
                "_make_api_request",
                side_effect=[{"records": [_cycle(id=1)], "next_token": "abc"}, RuntimeError("boom")],
            ),
            patch("app.services.providers.whoop.data_247.store_raw_payload"),
        ):
            cycles = data_247.get_cycle_data(
                MagicMock(),
                uuid4(),
                datetime(2026, 10, 1, tzinfo=timezone.utc),
                datetime(2026, 10, 30, tzinfo=timezone.utc),
            )
        assert [c["id"] for c in cycles] == [1]


class TestCycleSave:
    def test_saves_energy_and_strain(self, db: Session, data_247: Whoop247Data) -> None:
        user = UserFactory()
        UserConnectionFactory(user=user, provider="whoop")
        cycles = [_cycle(), _cycle(id=93846, end=None)]

        with patch.object(data_247, "get_cycle_data", return_value=cycles):
            saved = data_247.load_and_save_cycles(
                db, user.id, datetime(2026, 10, 27, tzinfo=timezone.utc), datetime(2026, 10, 30, tzinfo=timezone.utc)
            )

        assert saved == 1
        score = db.query(HealthScore).filter(HealthScore.user_id == user.id).one()
        assert score.category == HealthScoreCategory.STRAIN
        assert float(score.value) == pytest.approx(14.2)

        energy = (
            db.query(DataPointSeries)
            .filter(DataPointSeries.series_type_definition_id == get_series_type_id(SeriesType.energy))
            .one()
        )
        assert float(energy.value) == pytest.approx(2500.0)

    def test_load_and_save_all_reports_cycles(self, data_247: Whoop247Data) -> None:
        with (
            patch.object(data_247, "load_and_save_sleep", return_value=0),
            patch.object(data_247, "load_and_save_recovery", return_value=0),
            patch.object(data_247, "load_and_save_body_measurement", return_value=0),
            patch.object(data_247, "load_and_save_cycles", return_value=3) as cycles,
        ):
            results = data_247.load_and_save_all(MagicMock(), uuid4())
        assert results["cycles_synced"] == 3
        cycles.assert_called_once()

    def test_sleep_webhook_refreshes_recent_cycles(self) -> None:
        strategy = WhoopStrategy()
        handler = strategy.webhooks
        assert handler is not None
        with (
            patch.object(handler.data_247, "load_single_sleep", return_value=1),  # ty:ignore[unresolved-attribute]
            patch.object(handler.data_247, "refresh_recent_cycles", return_value=1) as refresh,  # ty:ignore[unresolved-attribute]
        ):
            handler._handle_updated(MagicMock(), WhoopWebhookNotificationType.SLEEP_UPDATED, uuid4(), "sleep-1")  # ty:ignore[unresolved-attribute]
        refresh.assert_called_once()


class TestCoverageAndScopes:
    def test_coverage(self) -> None:
        assert SeriesType.energy in TIMESERIES
        assert HealthScoreCategory.STRAIN in HEALTH_SCORES

    def test_default_scope_covers_endpoints_in_use(self) -> None:
        scopes = settings.whoop_default_scope.split()
        for scope in ("offline", "read:cycles", "read:sleep", "read:recovery", "read:workout"):
            assert scope in scopes
        # /v2/user/measurement/body and /v2/user/profile/basic are called by the code
        assert "read:body_measurement" in scopes
        assert "read:profile" in scopes
