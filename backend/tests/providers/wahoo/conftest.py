"""Wahoo Cloud API fixtures, shaped after the public examples at https://cloud-api.wahooligan.com/."""

from typing import Any

import pytest

from app.services.providers.factory import ProviderFactory
from app.services.providers.wahoo.strategy import WahooStrategy


def make_summary(**overrides: Any) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "id": 8297,
        "ascent_accum": "450.00",
        "cadence_avg": "50.00",
        "calories_accum": "1500.00",
        "distance_accum": "24909.71",
        "duration_active_accum": "179.00",
        "duration_paused_accum": "95.25",
        "duration_total_accum": "275.00",
        "heart_rate_avg": "100.00",
        "power_avg": "94.59",
        "power_bike_np_last": "150.00",
        "power_bike_tss_last": "304.90",
        "speed_avg": "10.75",
        "work_accum": "1041480.00",
        "time_zone": "Europe/Madrid",
        "manual": False,
        "edited": False,
        "fitness_app_id": 1002,
        "file": {"url": "https://cdn.wahooligan.com/4_Mile_Segment_.fit"},
        "created_at": "2026-07-12T09:10:00.000Z",
        "updated_at": "2026-07-12T09:10:00.000Z",
    }
    summary.update(overrides)
    return summary


def make_workout(workout_id: int = 56519, starts: str = "2026-07-12T09:00:00.000Z", **overrides: Any) -> dict:
    workout: dict[str, Any] = {
        "id": workout_id,
        "starts": starts,
        "minutes": 5,
        "name": "Saturday ride",
        "plan_id": None,
        "workout_token": "123",
        "workout_type_id": 15,
        "workout_summary": make_summary(),
        "created_at": starts,
        "updated_at": starts,
    }
    workout.update(overrides)
    return workout


@pytest.fixture
def wahoo_strategy() -> WahooStrategy:
    strategy = ProviderFactory().get_provider("wahoo")
    assert isinstance(strategy, WahooStrategy)
    return strategy


@pytest.fixture
def webhook_payload() -> dict[str, Any]:
    """``workout_summary`` webhook body (workout nested in the summary)."""
    summary = make_summary()
    summary["workout"] = {
        "id": 56519,
        "starts": "2026-07-12T09:00:00.000Z",
        "minutes": 5,
        "name": "Saturday ride",
        "plan_id": None,
        "workout_token": "123",
        "workout_type_id": 15,
    }
    return {
        "event_type": "workout_summary",
        "webhook_token": "wahoo-test-token",
        "user": {"id": 60462},
        "workout_summary": summary,
    }
