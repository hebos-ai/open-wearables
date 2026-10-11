"""Canonical PlannedWorkout → Wahoo plan.json, and the /v1/plans + /v1/workouts calls."""

import base64
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.models import PlannedWorkoutPush
from app.schemas.planned_workouts import PlannedWorkout, PlannedWorkoutPushRequest
from app.services.providers.templates.base_planned_workouts import (
    PlannedWorkoutDeliveryError,
    PlannedWorkoutTranslationError,
)
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


class FakePushes:
    """In-memory stand-in for PlannedWorkoutPushRepository (no DB in these unit tests)."""

    def __init__(self) -> None:
        self.rows: dict[tuple[UUID, str, str], PlannedWorkoutPush] = {}
        self.locked: list[str] = []

    @contextmanager
    def lock(self, db: Any, user_id: UUID, provider: str, external_id: str) -> Iterator[None]:
        self.locked.append(external_id)
        yield

    def get(self, db: Any, user_id: UUID, provider: str, external_id: str) -> PlannedWorkoutPush | None:
        return self.rows.get((user_id, provider, external_id))

    def save(
        self,
        db: Any,
        user_id: UUID,
        provider: str,
        external_id: str,
        provider_plan_id: str | None,
        provider_workout_id: str | None,
        scheduled_at: datetime | None,
    ) -> PlannedWorkoutPush:
        row = PlannedWorkoutPush(
            id=uuid4(),
            user_id=user_id,
            provider=provider,
            external_id=external_id,
            provider_plan_id=provider_plan_id,
            provider_workout_id=provider_workout_id,
            scheduled_at=scheduled_at,
        )
        self.rows[(user_id, provider, external_id)] = row
        return row

    def delete(self, db: Any, row: PlannedWorkoutPush) -> None:
        self.rows.pop((row.user_id, row.provider, row.external_id), None)


class FakeWahooApi:
    """Wahoo Cloud API in small: plans and workouts by id, 404 for unknown ids."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.forms: list[dict[str, Any] | None] = []
        self.plans: dict[str, dict[str, Any]] = {}
        self.workouts: dict[str, dict[str, Any]] = {}
        self.next_id = 100

    def __call__(
        self,
        db: Any,
        user_id: UUID,
        endpoint: str,
        method: str = "POST",
        json_data: Any = None,
        form_data: dict[str, Any] | None = None,
        expect_json: bool = True,
    ) -> Any:
        self.calls.append((method, endpoint))
        self.forms.append(form_data)
        kind, _, obj_id = endpoint.removeprefix("/v1/").partition("/")
        store = self.plans if kind == "plans" else self.workouts
        if method == "POST":
            new_id = str(self.next_id)
            self.next_id += 1
            store[new_id] = form_data or {}
            return {"id": int(new_id)}
        if obj_id not in store:
            raise HTTPException(status_code=404, detail="Wahoo API error: not found")
        if method == "PUT":
            store[obj_id] = form_data or {}
            return {"id": int(obj_id)}
        if method == "DELETE":
            del store[obj_id]
            return {"status_code": 204, "accepted": False}
        raise AssertionError(method)


@pytest.fixture
def planner(wahoo_strategy: WahooStrategy) -> WahooPlannedWorkouts:
    assert isinstance(wahoo_strategy.planned_workouts, WahooPlannedWorkouts)
    planner = wahoo_strategy.planned_workouts
    planner.pushes = FakePushes()  # type: ignore[assignment]
    return planner


# What clinic-back#524 sends with DEVICE_WORKOUT_PUSH_FORMAT=grupos: 5 × (3:30 + 3:00) by pace.
SERIES = {
    "name": "Series intensas",
    "sport": "run",
    "steps": [
        {"kind": "warmup", "durationSec": 900, "target": {"type": "pace", "low": 2.5, "high": 2.833}},
        {
            "kind": "repeat",
            "repetitions": 5,
            "steps": [
                {
                    "kind": "interval",
                    "durationSec": 210,
                    "target": {"type": "pace", "low": 3.333, "high": 3.6},
                    "name": "Trabajo",
                    "note": "Ritmo de 5 km",
                },
                {"kind": "recovery", "durationSec": 180, "target": {"type": "pace", "low": 1.833, "high": 2.5}},
            ],
        },
        {"kind": "cooldown", "durationSec": 600},
    ],
}


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

    def test_repeat_groups_and_step_texts(self) -> None:
        workout = PlannedWorkout.model_validate(SERIES)
        assert workout.steps[1].kind == "repeat"
        assert len(workout.flat_steps()) == 1 + 5 * 2 + 1
        assert workout.flat_steps()[1].name == "Trabajo"

    @pytest.mark.parametrize(
        "item",
        [
            {"kind": "repeat", "repetitions": 3, "steps": []},
            {"kind": "repeat", "repetitions": 0, "steps": [{"kind": "interval", "durationSec": 60}]},
            {
                "kind": "repeat",
                "repetitions": 2,
                "steps": [{"kind": "repeat", "repetitions": 2, "steps": [{"kind": "interval", "durationSec": 60}]}],
            },
            {"kind": "interval", "durationSec": 60, "note": "x" * 501},
        ],
    )
    def test_rejects_invalid_groups_and_texts(self, item: dict) -> None:
        with pytest.raises(ValidationError):
            PlannedWorkout.model_validate({"name": "x", "sport": "run", "steps": [item]})

    @pytest.mark.parametrize("external_id", ["a/b", "con espacio", ""])
    def test_external_id_must_be_url_safe(self, external_id: str) -> None:
        with pytest.raises(ValidationError):
            _request(SWEET_SPOT, external_id=external_id)

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

    def test_repeat_group_is_a_native_wahoo_repeat(self, planner: WahooPlannedWorkouts) -> None:
        request = _request(SERIES)
        out = planner.translate(request.workout, request)
        intervals = out["plan"]["intervals"]
        assert len(intervals) == 3
        assert intervals[1] == {
            "name": "5 x",
            "exit_trigger_type": "repeat",
            "exit_trigger_value": 5,
            "intensity_type": "active",
            "intervals": [
                {
                    "name": "Trabajo",
                    "exit_trigger_type": "time",
                    "exit_trigger_value": 210,
                    "intensity_type": "active",
                    "targets": [{"type": "speed", "low": 3.333, "high": 3.6}],
                },
                {
                    "name": "Recovery",
                    "exit_trigger_type": "time",
                    "exit_trigger_value": 180,
                    "intensity_type": "recover",
                    "targets": [{"type": "speed", "low": 1.833, "high": 2.5}],
                },
            ],
        }
        # Totals count every repetition.
        assert out["plan"]["header"]["duration_s"] == 900 + 5 * (210 + 180) + 600
        assert out["minutes"] == 58  # 3450 s

    def test_open_step_inside_a_group_names_its_position(self, planner: WahooPlannedWorkouts) -> None:
        workout = {
            "name": "x",
            "sport": "run",
            "steps": [
                {"kind": "warmup", "durationSec": 60},
                {
                    "kind": "repeat",
                    "repetitions": 2,
                    "steps": [{"kind": "interval", "durationSec": 60}, {"kind": "rest"}],
                },
            ],
        }
        request = _request(workout)
        with pytest.raises(PlannedWorkoutTranslationError, match="step 3"):
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

    def test_missing_plan_id_is_a_delivery_error(self, planner: WahooPlannedWorkouts) -> None:
        with (
            patch.object(planner, "_make_api_request", return_value={}),
            pytest.raises(PlannedWorkoutDeliveryError, match="plan id") as exc,
        ):
            planner.push(None, uuid4(), _request(SWEET_SPOT))  # type: ignore[arg-type]
        assert exc.value.provider_plan_id is None

    def test_plan_created_but_scheduling_fails_keeps_plan_id(self, planner: WahooPlannedWorkouts) -> None:
        request = _request(SWEET_SPOT, scheduled_at="2026-10-12T07:00:00+00:00")
        failure = HTTPException(status_code=422, detail="Wahoo API error: starts invalid")
        with (
            patch.object(planner, "_make_api_request", side_effect=[{"id": 777}, failure]),
            pytest.raises(PlannedWorkoutDeliveryError) as exc,
        ):
            planner.push(None, uuid4(), request)  # type: ignore[arg-type]
        assert exc.value.provider_plan_id == "777"

    def test_reauthorization_is_not_masked(self, planner: WahooPlannedWorkouts) -> None:
        request = _request(SWEET_SPOT, scheduled_at="2026-10-12T07:00:00+00:00")
        with (
            patch.object(planner, "_make_api_request", side_effect=[{"id": 777}, HTTPException(status_code=401)]),
            pytest.raises(HTTPException) as exc,
        ):
            planner.push(None, uuid4(), request)  # type: ignore[arg-type]
        assert exc.value.status_code == 401


class TestReplaceAndDelete:
    """Same external_id → the same Wahoo plan/workout is updated, never duplicated; delete removes both."""

    USER = uuid4()

    def _push(self, planner: WahooPlannedWorkouts, api: FakeWahooApi, **kwargs: object) -> Any:
        with patch.object(planner, "_make_api_request", side_effect=api):
            return planner.push(None, self.USER, _request(SERIES, external_id="hebos-tce-77", **kwargs))  # type: ignore[arg-type]

    def test_resend_updates_the_same_plan_and_workout(self, planner: WahooPlannedWorkouts) -> None:
        api = FakeWahooApi()
        first = self._push(planner, api, scheduled_at="2026-10-13T18:30:00+02:00")
        assert api.calls == [("POST", "/v1/plans"), ("POST", "/v1/workouts")]
        api.calls.clear()

        second = self._push(planner, api, scheduled_at="2026-10-14T18:30:00+02:00")
        assert api.calls == [
            ("PUT", f"/v1/plans/{first.provider_plan_id}"),
            ("PUT", f"/v1/workouts/{first.provider_workout_id}"),
        ]
        assert (second.provider_plan_id, second.provider_workout_id) == (
            first.provider_plan_id,
            first.provider_workout_id,
        )
        assert len(api.plans) == 1
        assert len(api.workouts) == 1
        assert api.workouts[first.provider_workout_id]["workout[starts]"] == "2026-10-14T18:30:00+02:00"
        assert planner.pushes.locked == ["hebos-tce-77", "hebos-tce-77"]  # type: ignore[attr-defined]

    def test_resend_recreates_what_the_athlete_deleted_on_wahoo(self, planner: WahooPlannedWorkouts) -> None:
        api = FakeWahooApi()
        self._push(planner, api, scheduled_at="2026-10-13T18:30:00+02:00")
        api.plans.clear()
        api.workouts.clear()
        api.calls.clear()

        result = self._push(planner, api, scheduled_at="2026-10-13T18:30:00+02:00")
        assert api.calls == [
            ("PUT", "/v1/plans/100"),
            ("POST", "/v1/plans"),
            ("PUT", "/v1/workouts/101"),
            ("POST", "/v1/workouts"),
        ]
        assert (result.provider_plan_id, result.provider_workout_id) == ("102", "103")

    def test_resend_without_date_takes_it_off_the_calendar(self, planner: WahooPlannedWorkouts) -> None:
        api = FakeWahooApi()
        self._push(planner, api, scheduled_at="2026-10-13T18:30:00+02:00")
        api.calls.clear()
        result = self._push(planner, api)
        assert api.calls == [("PUT", "/v1/plans/100"), ("DELETE", "/v1/workouts/101")]
        assert result.status == "created"
        assert api.workouts == {}
        row = planner.pushes.get(None, self.USER, "wahoo", "hebos-tce-77")  # type: ignore[arg-type]
        assert row is not None
        assert row.provider_workout_id is None

    def test_scheduling_failure_records_the_plan_so_a_retry_updates_it(self, planner: WahooPlannedWorkouts) -> None:
        api = FakeWahooApi()

        def failing(*args: Any, **kwargs: Any) -> Any:
            if args[2] == "/v1/workouts":
                raise HTTPException(status_code=503, detail="Wahoo API error: down")
            return api(*args, **kwargs)

        with (
            patch.object(planner, "_make_api_request", side_effect=failing),
            pytest.raises(PlannedWorkoutDeliveryError) as exc,
        ):
            planner.push(
                None, self.USER, _request(SERIES, external_id="hebos-tce-77", scheduled_at="2026-10-13T07:00:00+02:00")
            )  # type: ignore[arg-type]
        assert exc.value.provider_plan_id == "100"
        api.calls.clear()
        self._push(planner, api, scheduled_at="2026-10-13T07:00:00+02:00")
        assert api.calls == [("PUT", "/v1/plans/100"), ("POST", "/v1/workouts")]
        assert len(api.plans) == 1

    def test_delete_removes_workout_then_plan_and_is_idempotent(self, planner: WahooPlannedWorkouts) -> None:
        api = FakeWahooApi()
        self._push(planner, api, scheduled_at="2026-10-13T18:30:00+02:00")
        api.calls.clear()
        with patch.object(planner, "_make_api_request", side_effect=api):
            first = planner.delete(None, self.USER, "hebos-tce-77")  # type: ignore[arg-type]
            second = planner.delete(None, self.USER, "hebos-tce-77")  # type: ignore[arg-type]
        assert api.calls == [("DELETE", "/v1/workouts/101"), ("DELETE", "/v1/plans/100")]
        assert first.deleted is True
        assert second.deleted is False
        assert api.plans == {}
        assert api.workouts == {}

    def test_delete_tolerates_objects_already_gone(self, planner: WahooPlannedWorkouts) -> None:
        api = FakeWahooApi()
        self._push(planner, api, scheduled_at="2026-10-13T18:30:00+02:00")
        api.plans.clear()
        api.workouts.clear()
        with patch.object(planner, "_make_api_request", side_effect=api):
            assert planner.delete(None, self.USER, "hebos-tce-77").deleted is True  # type: ignore[arg-type]
        assert planner.pushes.get(None, self.USER, "wahoo", "hebos-tce-77") is None  # type: ignore[arg-type]

    def test_delete_keeps_the_record_when_wahoo_fails(self, planner: WahooPlannedWorkouts) -> None:
        api = FakeWahooApi()
        self._push(planner, api, scheduled_at="2026-10-13T18:30:00+02:00")
        boom = HTTPException(status_code=503, detail="Wahoo API error: down")
        with (
            patch.object(planner, "_make_api_request", side_effect=boom),
            pytest.raises(PlannedWorkoutDeliveryError, match="delete failed"),
        ):
            planner.delete(None, self.USER, "hebos-tce-77")  # type: ignore[arg-type]
        assert planner.pushes.get(None, self.USER, "wahoo", "hebos-tce-77") is not None  # type: ignore[arg-type]

    def test_provider_errors_other_than_401_become_delivery_errors(self, planner: WahooPlannedWorkouts) -> None:
        api = FakeWahooApi()
        self._push(planner, api, scheduled_at="2026-10-13T18:30:00+02:00")

        def rejects_plan_update(*args: Any, **kwargs: Any) -> Any:
            if kwargs.get("method") == "PUT":
                raise HTTPException(status_code=422, detail="Wahoo API error: invalid plan")
            return api(*args, **kwargs)

        with (
            patch.object(planner, "_make_api_request", side_effect=rejects_plan_update),
            pytest.raises(PlannedWorkoutDeliveryError, match="plan not saved") as exc,
        ):
            planner.push(None, self.USER, _request(SERIES, external_id="hebos-tce-77"))  # type: ignore[arg-type]
        assert exc.value.provider_plan_id == "100"

        unauthorized = HTTPException(status_code=401, detail="re-authorize")
        with (
            patch.object(planner, "_make_api_request", side_effect=unauthorized),
            pytest.raises(HTTPException) as exc401,
        ):
            planner.delete(None, self.USER, "hebos-tce-77")  # type: ignore[arg-type]
        assert exc401.value.status_code == 401

    def test_workout_without_id_is_a_delivery_error_and_not_reported_as_scheduled(
        self, planner: WahooPlannedWorkouts
    ) -> None:
        responses = iter([{"id": 777}, {}])
        with (
            patch.object(planner, "_make_api_request", side_effect=lambda *a, **k: next(responses)),
            pytest.raises(PlannedWorkoutDeliveryError, match="no workout id") as exc,
        ):
            planner.push(
                None,  # type: ignore[arg-type]
                self.USER,
                _request(SERIES, external_id="sin-id", scheduled_at="2026-10-13T07:00:00+02:00"),
            )
        assert exc.value.provider_plan_id == "777"
        row = planner.pushes.get(None, self.USER, "wahoo", "sin-id")  # type: ignore[arg-type]
        assert row is not None
        assert row.provider_workout_id is None
        assert row.scheduled_at is None

    def test_open_step_inside_a_group_names_the_group(self, planner: WahooPlannedWorkouts) -> None:
        workout = {
            "name": "x",
            "sport": "run",
            "steps": [
                {
                    "kind": "repeat",
                    "repetitions": 2,
                    "steps": [{"kind": "interval", "durationSec": 9}, {"kind": "rest"}],
                }
            ],
        }
        request = _request(workout)
        with pytest.raises(PlannedWorkoutTranslationError, match=r"step 2 \(repeat group 1, step 2\)"):
            planner.translate(request.workout, request)
