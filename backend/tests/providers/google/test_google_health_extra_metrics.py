"""Google Health API: height, nightly skin temperature (+ deviation), daily SpO2 / HRV,
vo2-max and nutrition-log → dietary_* series."""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import DataPointSeries
from app.schemas.enums import SeriesType, get_series_type_id
from app.services.providers.google.coverage import TIMESERIES
from app.services.providers.google.health_api.data_247 import GoogleHealth247Data
from app.services.providers.google.health_api.metrics import METRICS
from app.services.providers.google.health_api.nutrition import NUTRITION_DATA_TYPE, nutrients_of
from app.services.providers.google.health_api.webhook_service import GOOGLE_WEBHOOK_DATA_TYPES
from tests.factories import UserFactory

START = datetime(2026, 10, 1, tzinfo=timezone.utc)
END = datetime(2026, 10, 3, tzinfo=timezone.utc)


def _handler() -> GoogleHealth247Data:
    return GoogleHealth247Data(oauth=MagicMock(), connection_repo=MagicMock(), api_base_url="https://health.test")


def _metric(data_type: str) -> Any:
    return next(m for m in METRICS if m.data_type == data_type)


def _native(points: list[dict[str, Any]], data_type: str) -> list[Any]:
    handler = _handler()
    with (
        patch.object(handler, "_fetch_points", return_value=points),
        patch("app.services.providers.google.health_api.data_247.settings.google_use_reconcile", False),
    ):
        return handler._native_samples(MagicMock(), UserFactory.build().id, _metric(data_type), START, END)


def _by_series(samples: list[Any]) -> dict[SeriesType, Decimal]:
    return {s.series_type: Decimal(str(s.value)) for s in samples}


class TestRegistry:
    @pytest.mark.parametrize(
        "data_type",
        [
            "height",
            "daily-sleep-temperature-derivations",
            "daily-oxygen-saturation",
            "daily-heart-rate-variability",
            "vo2-max",
        ],
    )
    def test_new_data_types_are_registered(self, data_type: str) -> None:
        assert _metric(data_type)

    def test_coverage_lists_new_series(self) -> None:
        for series in (
            SeriesType.height,
            SeriesType.skin_temperature,
            SeriesType.skin_temperature_deviation,
            SeriesType.dietary_energy_consumed,
            SeriesType.dietary_protein,
        ):
            assert series in TIMESERIES

    def test_skin_temperature_never_goes_to_body_temperature(self) -> None:
        metric = _metric("daily-sleep-temperature-derivations")
        assert SeriesType.body_temperature not in metric.series_types()
        assert metric.series_types() == {SeriesType.skin_temperature, SeriesType.skin_temperature_deviation}

    def test_webhook_subscribes_new_types(self) -> None:
        for data_type in ("height", "daily-sleep-temperature-derivations", "daily-oxygen-saturation", "nutrition-log"):
            assert data_type in GOOGLE_WEBHOOK_DATA_TYPES


class TestListParsing:
    def test_height_millimeters_to_cm(self) -> None:
        point = {
            "height": {
                "sampleTime": {"physicalTime": "2026-10-01T08:00:00Z", "utcOffset": "7200s"},
                "heightMillimeters": 1782,
            }
        }
        samples = _native([point], "height")
        assert _by_series(samples) == {SeriesType.height: Decimal("178.2")}

    def test_sleep_temperature_emits_absolute_and_deviation(self) -> None:
        point = {
            "dailySleepTemperatureDerivations": {
                "date": {"year": 2026, "month": 10, "day": 2},
                "nightlyTemperatureCelsius": 34.9,
                "baselineTemperatureCelsius": 34.6,
                "relativeNightlyStddev30dCelsius": 0.2,
            }
        }
        samples = _native([point], "daily-sleep-temperature-derivations")
        values = _by_series(samples)
        assert values[SeriesType.skin_temperature] == Decimal("34.9")
        assert values[SeriesType.skin_temperature_deviation].quantize(Decimal("0.01")) == Decimal("0.30")
        assert all(s.is_daily_total for s in samples)

    def test_zero_deviation_is_kept(self) -> None:
        point = {
            "dailySleepTemperatureDerivations": {
                "date": {"year": 2026, "month": 10, "day": 2},
                "nightlyTemperatureCelsius": 34.6,
                "baselineTemperatureCelsius": 34.6,
            }
        }
        values = _by_series(_native([point], "daily-sleep-temperature-derivations"))
        assert values[SeriesType.skin_temperature_deviation] == 0

    def test_no_baseline_no_deviation(self) -> None:
        point = {
            "dailySleepTemperatureDerivations": {
                "date": {"year": 2026, "month": 10, "day": 2},
                "nightlyTemperatureCelsius": 34.6,
            }
        }
        values = _by_series(_native([point], "daily-sleep-temperature-derivations"))
        assert set(values) == {SeriesType.skin_temperature}

    def test_daily_oxygen_saturation(self) -> None:
        point = {"dailyOxygenSaturation": {"date": {"year": 2026, "month": 10, "day": 1}, "averagePercentage": 96.5}}
        assert _by_series(_native([point], "daily-oxygen-saturation")) == {
            SeriesType.oxygen_saturation: Decimal("96.5")
        }

    def test_daily_hrv_is_rmssd(self) -> None:
        point = {
            "dailyHeartRateVariability": {
                "date": {"year": 2026, "month": 10, "day": 1},
                "averageHeartRateVariabilityMilliseconds": 41.2,
            }
        }
        values = _by_series(_native([point], "daily-heart-rate-variability"))
        assert values == {SeriesType.heart_rate_variability_rmssd: Decimal("41.2")}

    def test_vo2_max(self) -> None:
        point = {"vo2Max": {"sampleTime": {"physicalTime": "2026-10-01T08:00:00Z"}, "vo2Max": 48.0}}
        assert _by_series(_native([point], "vo2-max")) == {SeriesType.vo2_max: Decimal("48.0")}

    def test_points_outside_window_are_dropped(self) -> None:
        point = {"height": {"sampleTime": {"physicalTime": "2026-09-20T08:00:00Z"}, "heightMillimeters": 1782}}
        assert _native([point], "height") == []


def _nutrition_point(start: str = "2026-10-01T13:00:00Z") -> dict[str, Any]:
    return {
        "name": "users/me/dataTypes/nutrition-log/dataPoints/abc",
        "dataSource": {"device": {"displayName": "Pixel 9"}},
        "nutritionLog": {
            "interval": {"startTime": start, "endTime": "2026-10-01T13:30:00Z", "startUtcOffset": "7200s"},
            "foodDisplayName": "Paella",
            "mealType": "LUNCH",
            "energy": {"kcal": 650},
            "totalCarbohydrate": {"grams": 80},
            "totalFat": {"grams": 22},
            "nutrients": [
                {"nutrient": "PROTEIN", "quantity": {"grams": 31}},
                {"nutrient": "SODIUM", "quantity": {"grams": 1.2}},
                {"nutrient": "VITAMIN_D", "quantity": {"grams": 0.000005}},
                {"nutrient": "CARBOHYDRATES", "quantity": {"grams": 99}},
                {"nutrient": "UNKNOWN_THING", "quantity": {"grams": 3}},
            ],
        },
    }


class TestNutrition:
    def test_nutrients_scaled_to_series_unit(self) -> None:
        values = nutrients_of(_nutrition_point()["nutritionLog"])
        assert values[SeriesType.dietary_energy_consumed] == 650
        assert values[SeriesType.dietary_protein] == 31
        assert values[SeriesType.dietary_fat_total] == 22
        assert values[SeriesType.dietary_sodium] == Decimal("1200.0")  # g -> mg
        assert values[SeriesType.dietary_vitamin_d].quantize(Decimal("0.01")) == Decimal("5.00")  # g -> mcg
        # Top-level total wins over the repeated nutrient
        assert values[SeriesType.dietary_carbohydrates] == 80
        assert len(values) == 6

    def test_nutrition_samples_from_list(self) -> None:
        handler = _handler()
        user_id = UserFactory.build().id
        outside = _nutrition_point("2026-09-01T13:00:00Z")
        with patch.object(handler, "_fetch_points", return_value=[_nutrition_point(), outside]) as fetch:
            samples = handler._nutrition_samples(MagicMock(), user_id, START, END)
        endpoint, time_filter = fetch.call_args.args[2], fetch.call_args.args[3]
        assert endpoint == "/v4/users/me/dataTypes/nutrition-log/dataPoints"
        assert "nutrition_log.interval.civil_start_time" in time_filter
        assert len(samples) == 6
        assert {s.recorded_at for s in samples} == {datetime(2026, 10, 1, 13, tzinfo=timezone.utc)}
        assert all(not s.is_daily_total and s.zone_offset == "+02:00" for s in samples)


class TestPersistence:
    def test_sync_nutrition_and_temperature_store_series(self, db: Session) -> None:
        user = UserFactory()
        handler = _handler()
        temp_point = {
            "dailySleepTemperatureDerivations": {
                "date": {"year": 2026, "month": 10, "day": 2},
                "nightlyTemperatureCelsius": 34.9,
                "baselineTemperatureCelsius": 34.6,
            }
        }
        with patch.object(handler, "_fetch_points", return_value=[_nutrition_point()]):
            assert handler.sync_data_type(db, user.id, NUTRITION_DATA_TYPE, START, END) == 6
        with (
            patch.object(handler, "_fetch_points", return_value=[temp_point]),
            patch("app.services.providers.google.health_api.data_247.settings.google_use_reconcile", False),
        ):
            assert handler.sync_data_type(db, user.id, "daily-sleep-temperature-derivations", START, END) == 2

        stored = {
            row.series_type_definition_id: row.value
            for row in db.query(DataPointSeries).all()
            if row.series_type_definition_id
            in {
                get_series_type_id(SeriesType.dietary_energy_consumed),
                get_series_type_id(SeriesType.skin_temperature_deviation),
            }
        }
        assert stored[get_series_type_id(SeriesType.dietary_energy_consumed)] == 650
        assert Decimal(str(stored[get_series_type_id(SeriesType.skin_temperature_deviation)])).quantize(
            Decimal("0.1")
        ) == Decimal("0.3")
