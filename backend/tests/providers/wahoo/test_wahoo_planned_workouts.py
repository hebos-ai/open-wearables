"""Canonical PlannedWorkout → Wahoo plan.json, and the /v1/plans + /v1/workouts calls."""

import base64
import json
from datetime import datetime, timezone
from unittest.mock import patch
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.planned_workouts import PlannedWorkout, PlannedWorkoutPushRequest
from app.services.providers.templates.base_planned_workouts import PlannedWorkoutTranslationError
from app.services.providers.wahoo.planned_workouts import WahooPlannedWorkouts
from app.services.providers.wahoo.strategy import WahooStrategy

# Same JSON a garmin-connector / welabox client sends (camelCase keys).
SWEET_SPOT = {
    "name": "Sweet spot 3x10",
    "sport": "bike",
    "steps": [
        {"kind": "warmup", "durationSec": 600, "target": {"type": "power", "low": 120, "high": 160}},
        {"kind": "interval", "durationSec": 600, "target": {"type": "power", "low": 230, "high": 245}},
        {"kind": "recovery", "durationSec": 300, "target": {"type": "power", "high": 140}},
        {"kind": "interval", "durationSec": 600, "target": {"type": "hr", "low": 150, "high": 160}},
        {"kind": "cooldown", "durationSec": 420},
    ],
}


@pytest.fixture
def planner(wahoo_strategy: WahooStrategy) -> WahooPlannedWorkouts:
    assert isinstance(wahoo_strategy.planned_workouts, WahooPlannedWorkouts)
    return wahoo_strategy.planned_workouts


def _request(workout: dict, **kwargs: object) -> PlannedWorkoutPushRequest:
    return PlannedWorkoutPushRequest.model_validate({"workout": workout, **kwargs})


class TestCanonicalModel:
    def test_accepts_camel_and_snake_case(self) -> None:
        camel = PlannedWorkout.model_validate(SWEET_SPOT)
        snake = PlannedWorkout.model_validate(
            {"name": "x", "sport": "run", "steps": [{"kind": "interval", "duration_sec": 60}]}
        )
        assert camel.steps[0].duration_sec == 600
        assert snake.steps[0].duration_sec == 60

    @pytest.mark.parametrize(
        "step",
        [
            {"kind": "interval", "durationSec": 60, "distanceM": 400},
            {"kind": "interval", "durationSec": 60, "target": {"type": "power"}},
            {"kind": "interval", "durationSec": 60, "target": {"type": "power", "low": 300, "high": 200}},
            {"kind": "sprint", "durationSec": 60},
            {"kind": "interval", "durationSec": -5},
        ],
    )
    def test_rejects_invalid_steps(self, step: dict) -> None:
        with pytest.raises(ValidationError):
            PlannedWorkout.model_validate({"name": "x", "sport": "bike", "steps": [step]})

    def test_scheduled_at_must_be_timezone_aware(self) -> None:
        with pytest.raises(ValidationError):
            _request(SWEET_SPOT, scheduled_at="2026-10-12T07:00:00")


class TestTranslate:
    def test_bike_power_and_hr_targets(self, planner: WahooPlannedWorkouts) -> None:
        request = _request(SWEET_SPOT, description="Tempo build")
        out = planner.translate(request.workout, request)

        header = out["plan"]["header"]
        assert header == {
            "name": "Sweet spot 3x10",
            "version": "1.0.0",
            "workout_type_family": 0,
            "workout_type_location": 1,
            "description": "Tempo build",
            "duration_s": 2520,
        }
        intervals = out["plan"]["intervals"]
        assert intervals[0] == {
            "name": "Warm up",
            "exit_trigger_type": "time",
            "exit_trigger_value": 600,
            "intensity_type": "wu",
            "targets": [{"type": "watts", "low": 120, "high": 160}],
        }
        assert intervals[1]["intensity_type"] == "active"
        # single bound → low == high
        assert intervals[2]["targets"] == [{"type": "watts", "low": 140, "high": 140}]
        assert intervals[2]["intensity_type"] == "recover"
        assert intervals[3]["targets"] == [{"type": "hr", "low": 150, "high": 160}]
        assert intervals[4] == {
            "name": "Cool down",
            "exit_trigger_type": "time",
            "exit_trigger_value": 420,
            "intensity_type": "cd",
        }
        assert out["workout_type_id"] == 0
        assert out["minutes"] == 42

    def test_run_with_distance_and_pace(self, planner: WahooPlannedWorkouts) -> None:
        workout = {
            "name": "6x400",
            "sport": "running",
            "steps": [{"kind": "interval", "distanceM": 400, "target": {"type": "pace", "low": 4.8, "high": 5.2}}],
        }
        request = _request(workout)
        out = planner.translate(request.workout, request)

        assert out["plan"]["header"]["workout_type_family"] == 1
        assert out["plan"]["header"]["distance_m"] == 400
        assert "duration_s" not in out["plan"]["header"]
        assert out["plan"]["intervals"][0]["exit_trigger_type"] == "distance"
        assert out["plan"]["intervals"][0]["targets"] == [{"type": "speed", "low": 4.8, "high": 5.2}]
        assert out["workout_type_id"] == 1
        assert out["minutes"] == 2  # 400 m at 5 m/s = 80 s → rounded up

    def test_indoor_sports_use_indoor_location(self, planner: WahooPlannedWorkouts) -> None:
        workout = {"name": "Trainer", "sport": "indoor_cycling", "steps": [{"kind": "interval", "durationSec": 60}]}
        request = _request(workout)
        out = planner.translate(request.workout, request)
        assert out["plan"]["header"]["workout_type_location"] == 0
        assert out["workout_type_id"] == 61

    @pytest.mark.parametrize("sport", ["swim", "strength", "brick"])
    def test_unsupported_sport(self, planner: WahooPlannedWorkouts, sport: str) -> None:
        request = _request({"name": "x", "sport": sport, "steps": [{"kind": "interval", "durationSec": 60}]})
        with pytest.raises(PlannedWorkoutTranslationError, match="not supported"):
            planner.translate(request.workout, request)

    def test_open_step_is_rejected(self, planner: WahooPlannedWorkouts) -> None:
        request = _request({"name": "x", "sport": "bike", "steps": [{"kind": "interval"}]})
        with pytest.raises(PlannedWorkoutTranslationError, match="step 1"):
            planner.translate(request.workout, request)

    def test_plan_file_is_base64_json_data_uri(self) -> None:
        uri = WahooPlannedWorkouts.encode_plan_file({"header": {"name": "x"}, "intervals": []})
        prefix = "data:application/json;base64,"
        assert uri.startswith(prefix)
        assert json.loads(base64.b64decode(uri[len(prefix) :])) == {"header": {"name": "x"}, "intervals": []}


class TestSend:
    def test_library_only_creates_plan(self, planner: WahooPlannedWorkouts) -> None:
        request = _request(SWEET_SPOT, external_id="hebos:1:2:entry-9")
        with patch.object(planner, "_make_api_request", return_value={"id": 777}) as api:
            result = planner.push(None, uuid4(), request)  # type: ignore[arg-type]

        assert api.call_count == 1
        assert api.call_args.args[2] == "/v1/plans"
        form = api.call_args.kwargs["form_data"]
        assert form["plan[external_id]"] == "hebos:1:2:entry-9"
        assert form["plan[filename]"] == "plan.json"
        assert form["plan[file]"].startswith("data:application/json;base64,")
        assert "plan[provider_updated_at]" in form
        assert result.status == "created"
        assert result.provider_plan_id == "777"
        assert result.provider_workout_id is None

    def test_scheduled_creates_plan_then_workout(self, planner: WahooPlannedWorkouts) -> None:
        when = datetime(2026, 10, 12, 7, 0, tzinfo=timezone.utc)
        request = _request(SWEET_SPOT, scheduled_at=when.isoformat(), external_id="ext-1")
        with patch.object(planner, "_make_api_request", side_effect=[{"id": 777}, {"id": 4242}]) as api:
            result = planner.push(None, uuid4(), request)  # type: ignore[arg-type]

        workout_call = api.call_args_list[1]
        assert workout_call.args[2] == "/v1/workouts"
        assert workout_call.kwargs["form_data"] == {
            "workout[plan_id]": 777,
            "workout[starts]": "2026-10-12T07:00:00+00:00",
            "workout[workout_type_id]": 0,
            "workout[name]": "Sweet spot 3x10",
            "workout[minutes]": 42,
            "workout[workout_token]": "ext-1",
        }
        assert result.status == "scheduled"
        assert result.provider_workout_id == "4242"
        assert result.scheduled_at == when

    def test_generates_external_id_when_missing(self, planner: WahooPlannedWorkouts) -> None:
        with patch.object(planner, "_make_api_request", return_value={"id": 1}):
            result = planner.push(None, uuid4(), _request(SWEET_SPOT))  # type: ignore[arg-type]
        assert result.external_id

    def test_missing_plan_id_is_an_error(self, planner: WahooPlannedWorkouts) -> None:
        with (
            patch.object(planner, "_make_api_request", return_value={}),
            pytest.raises(ValueError, match="plan id"),
        ):
            planner.push(None, uuid4(), _request(SWEET_SPOT))  # type: ignore[arg-type]
