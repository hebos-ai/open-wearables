"""Polar physical information (GET /v3/users/physical-info, webhook PHYSICAL_INFORMATION)."""

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

import pytest

from app.schemas.enums import SeriesType
from app.schemas.providers.polar import PolarWebhookEventType
from app.services.providers.polar.coverage import TIMESERIES
from app.services.providers.polar.data_247 import Polar247Data
from app.services.providers.polar.strategy import PolarStrategy

PHYSICAL_INFO = {
    "created": "2026-10-01T08:00:00Z",
    "modified": "2026-10-20T07:30:00Z",
    "weight": 61.5,
    "height": 168,
    "birthday": "1995-01-01",
    "gender": "FEMALE",
    "maximum_heart_rate": 191,
    "resting_heart_rate": 48,
    "aerobic_threshold": 150,
    "anaerobic_threshold": 172,
    "vo2_max": 52,
    "weight_source": "SOURCE_USER",
}


@pytest.fixture
def data_247() -> Polar247Data:
    return PolarStrategy().data_247


class TestNormalizePhysicalInfo:
    def test_maps_profile_values_to_series(self, data_247: Polar247Data) -> None:
        user_id = uuid4()
        samples = data_247.normalize_physical_info(PHYSICAL_INFO, user_id)
        by_type = {s.series_type: s for s in samples}

        assert by_type[SeriesType.weight].value == Decimal("61.5")
        assert by_type[SeriesType.height].value == Decimal("168")
        assert by_type[SeriesType.vo2_max].value == Decimal("52")
        assert by_type[SeriesType.max_heart_rate].value == Decimal("191")
        assert by_type[SeriesType.resting_heart_rate].value == Decimal("48")
        assert len(samples) == 5
        assert all(s.recorded_at == datetime(2026, 10, 20, 7, 30, tzinfo=timezone.utc) for s in samples)
        assert all(s.user_id == user_id and s.provider == "polar" for s in samples)

    def test_personal_fields_are_not_stored(self, data_247: Polar247Data) -> None:
        samples = data_247.normalize_physical_info(PHYSICAL_INFO, uuid4())
        assert not any("birthday" in (s.external_id or "") or "gender" in (s.external_id or "") for s in samples)

    def test_legacy_kebab_case_keys(self, data_247: Polar247Data) -> None:
        raw = {"created": "2026-10-01T08:00:00", "maximum-heart-rate": 185, "vo2-max": 47}
        samples = data_247.normalize_physical_info(raw, uuid4())
        assert {s.series_type for s in samples} == {SeriesType.max_heart_rate, SeriesType.vo2_max}
        assert samples[0].recorded_at.tzinfo is not None

    def test_missing_or_empty_values_are_skipped(self, data_247: Polar247Data) -> None:
        raw = {"modified": "2026-10-20T07:30:00Z", "weight": None, "height": 0, "vo2_max": "x"}
        assert data_247.normalize_physical_info(raw, uuid4()) == []
        assert data_247.normalize_physical_info({"weight": 60}, uuid4()) == []  # no timestamp
        assert data_247.normalize_physical_info(None, uuid4()) == []

    def test_coverage_declares_the_series(self) -> None:
        for series_type in (
            SeriesType.weight,
            SeriesType.height,
            SeriesType.vo2_max,
            SeriesType.max_heart_rate,
            SeriesType.resting_heart_rate,
        ):
            assert series_type in TIMESERIES


class TestPhysicalInfoFlow:
    def test_webhook_event_fetches_and_saves(self, data_247: Polar247Data) -> None:
        with (
            patch.object(data_247, "_make_api_request", return_value=PHYSICAL_INFO) as api,
            patch.object(data_247, "_save_timeseries", return_value=5) as save,
        ):
            result = data_247.fetch_and_save_from_webhook(
                db=None,  # ty:ignore[invalid-argument-type]
                user_id=uuid4(),
                event_type=PolarWebhookEventType.PHYSICAL_INFORMATION,
                path="/v3/users/physical-info",
            )
        assert result == {"physical_info": 5}
        api.assert_called_once()
        assert len(save.call_args.args[1]) == 5

    def test_sync_reads_physical_info(self, data_247: Polar247Data) -> None:
        with patch.object(data_247, "_make_api_request", return_value=PHYSICAL_INFO) as api:
            assert data_247.get_physical_info(None, uuid4()) == PHYSICAL_INFO  # ty:ignore[invalid-argument-type]
        assert api.call_args.args[2] == "/v3/users/physical-info"

    def test_non_dict_response_is_ignored(self, data_247: Polar247Data) -> None:
        with patch.object(data_247, "_make_api_request", return_value=None):
            assert data_247.get_physical_info(None, uuid4()) is None  # ty:ignore[invalid-argument-type]
