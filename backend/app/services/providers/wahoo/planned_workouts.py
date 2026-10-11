"""Push planned workouts to Wahoo (``POST /v1/plans`` + optional ``POST /v1/workouts``).

Canonical ``PlannedWorkout`` → Wahoo *plan.json* v1.0.0
(https://cloud-api.wahooligan.com/docs/plan-json-format.pdf):

==================  =========================================================
canonical           plan.json
==================  =========================================================
sport               header.workout_type_family (0 biking, 1 running) +
                    header.workout_type_location (0 indoor, 1 outdoor)
step.kind           interval.intensity_type (wu / active / recover / cd / rest)
step.name           interval.name (default: the kind, e.g. "Warm up")
repeat group        parent interval with exit_trigger_type "repeat",
                    exit_trigger_value = repetitions, and the group's steps as
                    nested ``intervals`` (native Wahoo repeat, not flattened)
step.duration_sec   exit_trigger_type "time", exit_trigger_value seconds
step.distance_m     exit_trigger_type "distance", exit_trigger_value metres
target power        target type "watts"
target hr           target type "hr"
target pace (m/s)   target type "speed" (m/s)
==================  =========================================================

Wahoo plans only support biking and running, and every interval needs an exit trigger,
so other sports and open steps are rejected (HTTP 422) instead of being approximated.

Without ``scheduled_at`` the plan is only added to the athlete's Wahoo plan library; with
it, a workout referencing the plan is created on that date (shows up on ELEMNT / the app).
plan.json has no per-step note field, so ``step.note`` is not sent to Wahoo.

Re-sending the same ``external_id`` updates the same plan (``PUT /v1/plans/{id}``) and workout
(``PUT /v1/workouts/{id}``), re-creating only what the athlete deleted on Wahoo (404); without
``scheduled_at`` an existing scheduled workout is removed. Delete removes the workout, then the plan.
"""

import base64
import json
import math
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import HTTPException

from app.database import DbSession
from app.models import PlannedWorkoutPush
from app.schemas.planned_workouts import (
    PlannedWorkout,
    PlannedWorkoutPushRequest,
    PlannedWorkoutPushResult,
    PlannedWorkoutRepeat,
    PlannedWorkoutStep,
)
from app.services.providers.templates.base_planned_workouts import (
    BasePlannedWorkoutsTemplate,
    PlannedWorkoutDeliveryError,
    PlannedWorkoutTranslationError,
)

PLAN_FORMAT_VERSION = "1.0.0"

FAMILY_BIKING = 0
FAMILY_RUNNING = 1
LOCATION_INDOOR = 0
LOCATION_OUTDOOR = 1

# canonical sport → (workout_type_family, workout_type_location, workout_type_id used when scheduling)
SPORTS: dict[str, tuple[int, int, int]] = {
    "bike": (FAMILY_BIKING, LOCATION_OUTDOOR, 0),  # BIKING
    "cycling": (FAMILY_BIKING, LOCATION_OUTDOOR, 0),
    "road_biking": (FAMILY_BIKING, LOCATION_OUTDOOR, 15),  # BIKING_ROAD
    "mtb": (FAMILY_BIKING, LOCATION_OUTDOOR, 13),  # BIKING_MOUNTAIN
    "mountain_biking": (FAMILY_BIKING, LOCATION_OUTDOOR, 13),
    "indoor_cycling": (FAMILY_BIKING, LOCATION_INDOOR, 61),  # BIKING_INDOOR_TRAINER
    "virtual_ride": (FAMILY_BIKING, LOCATION_INDOOR, 68),  # BIKING_INDOOR_VIRTUAL
    "run": (FAMILY_RUNNING, LOCATION_OUTDOOR, 1),  # RUNNING
    "running": (FAMILY_RUNNING, LOCATION_OUTDOOR, 1),
    "trail_running": (FAMILY_RUNNING, LOCATION_OUTDOOR, 4),  # RUNNING_TRAIL
    "treadmill": (FAMILY_RUNNING, LOCATION_INDOOR, 5),  # RUNNING_TREADMILL
}

INTENSITY: dict[str, str] = {
    "warmup": "wu",
    "interval": "active",
    "recovery": "recover",
    "cooldown": "cd",
    "rest": "rest",
}

STEP_NAMES: dict[str, str] = {
    "warmup": "Warm up",
    "interval": "Interval",
    "recovery": "Recovery",
    "cooldown": "Cool down",
    "rest": "Rest",
}

TARGET_TYPES: dict[str, str] = {"power": "watts", "hr": "hr", "pace": "speed"}


def _number(value: float) -> int | float:
    """Integers stay integers in the JSON (``300`` rather than ``300.0``)."""
    return int(value) if float(value).is_integer() else round(value, 3)


def _estimated_seconds(step: PlannedWorkoutStep) -> float:
    """Best-effort step duration, used only for the scheduled workout's ``minutes``."""
    if step.duration_sec is not None:
        return float(step.duration_sec)
    if step.distance_m is not None and step.target is not None and step.target.type == "pace":
        low, high = step.target.bounds
        return step.distance_m / ((low + high) / 2)
    return 0.0


class WahooPlannedWorkouts(BasePlannedWorkoutsTemplate):
    """Wahoo implementation of the planned-workout push capability."""

    # ------------------------------------------------------------------
    # Translation (pure)
    # ------------------------------------------------------------------

    def _sport(self, workout: PlannedWorkout) -> tuple[int, int, int]:
        key = workout.sport.strip().lower().replace("-", "_").replace(" ", "_")
        if key not in SPORTS:
            raise PlannedWorkoutTranslationError(
                "wahoo", f"sport '{workout.sport}' is not supported (Wahoo plans only support biking and running)"
            )
        return SPORTS[key]

    @staticmethod
    def _interval(index: int, step: PlannedWorkoutStep) -> dict[str, Any]:
        if step.duration_sec is not None:
            trigger_type, trigger_value = "time", _number(step.duration_sec)
        elif step.distance_m is not None:
            trigger_type, trigger_value = "distance", _number(step.distance_m)
        else:
            raise PlannedWorkoutTranslationError(
                "wahoo", f"step {index + 1} has no duration_sec or distance_m (Wahoo has no open steps)"
            )

        interval: dict[str, Any] = {
            "name": step.name or STEP_NAMES[step.kind],
            "exit_trigger_type": trigger_type,
            "exit_trigger_value": trigger_value,
            "intensity_type": INTENSITY[step.kind],
        }
        if step.target is not None:
            low, high = step.target.bounds
            interval["targets"] = [{"type": TARGET_TYPES[step.target.type], "low": _number(low), "high": _number(high)}]
        return interval

    def _intervals(self, workout: PlannedWorkout) -> list[dict[str, Any]]:
        intervals: list[dict[str, Any]] = []
        index = 0  # step number in the canonical workout, for error messages
        for item in workout.steps:
            if isinstance(item, PlannedWorkoutRepeat):
                children = []
                for step in item.steps:
                    children.append(self._interval(index, step))
                    index += 1
                intervals.append(
                    {
                        "name": f"{item.repetitions} x",
                        "exit_trigger_type": "repeat",
                        "exit_trigger_value": item.repetitions,
                        "intensity_type": "active",
                        "intervals": children,
                    }
                )
            else:
                intervals.append(self._interval(index, item))
                index += 1
        return intervals

    def translate(self, workout: PlannedWorkout, request: PlannedWorkoutPushRequest) -> dict[str, Any]:
        family, location, workout_type_id = self._sport(workout)
        intervals = self._intervals(workout)
        flat = workout.flat_steps()

        header: dict[str, Any] = {
            "name": workout.name,
            "version": PLAN_FORMAT_VERSION,
            "workout_type_family": family,
            "workout_type_location": location,
        }
        if request.description:
            header["description"] = request.description
        if all(step.duration_sec is not None for step in flat):
            header["duration_s"] = sum(step.duration_sec or 0 for step in flat)
        if all(step.distance_m is not None for step in flat):
            header["distance_m"] = _number(sum(step.distance_m or 0 for step in flat))

        estimated = sum(_estimated_seconds(step) for step in flat)
        return {
            "plan": {"header": header, "intervals": intervals},
            "workout_type_id": workout_type_id,
            "minutes": max(1, math.ceil(estimated / 60)),
        }

    # ------------------------------------------------------------------
    # Delivery
    # ------------------------------------------------------------------

    @staticmethod
    def encode_plan_file(plan: dict[str, Any]) -> str:
        """plan.json as the base64 data URI Wahoo expects in ``plan[file]``."""
        encoded = base64.b64encode(json.dumps(plan, separators=(",", ":")).encode()).decode()
        return f"data:application/json;base64,{encoded}"

    def _gone(self, call: Any) -> bool:
        """Run a provider call; True if the object no longer exists there (404)."""
        try:
            call()
        except HTTPException as exc:
            if exc.status_code == 404:
                return True
            raise
        return False

    def send(
        self,
        db: DbSession,
        user_id: UUID,
        payload: dict[str, Any],
        request: PlannedWorkoutPushRequest,
        external_id: str,
        existing: PlannedWorkoutPush | None,
    ) -> PlannedWorkoutPushResult:
        plan_form = {
            "plan[file]": self.encode_plan_file(payload["plan"]),
            "plan[filename]": "plan.json",
            "plan[external_id]": external_id,
            "plan[provider_updated_at]": datetime.now(timezone.utc).isoformat(),
        }
        plan_id = existing.provider_plan_id if existing else None
        workout_id = existing.provider_workout_id if existing else None
        scheduled_before = existing.scheduled_at if existing else None

        # 1. The plan: update the one we already sent; create it only if there is none (or it is gone).
        if plan_id is not None and self._gone(
            lambda: self._make_api_request(db, user_id, f"/v1/plans/{plan_id}", method="PUT", form_data=plan_form)
        ):
            plan_id = None
        if plan_id is None:
            plan_response = self._make_api_request(db, user_id, "/v1/plans", form_data=plan_form)
            new_id = plan_response.get("id") if isinstance(plan_response, dict) else None
            if new_id is None:
                raise PlannedWorkoutDeliveryError("wahoo", "POST /v1/plans returned no plan id")
            plan_id = str(new_id)
            self._record(db, user_id, external_id, plan_id, workout_id, scheduled_before)

        # 2. Library only: an earlier scheduled workout for this id is taken off the calendar.
        if request.scheduled_at is None:
            if workout_id is not None:
                self._gone(
                    lambda: self._make_api_request(
                        db, user_id, f"/v1/workouts/{workout_id}", method="DELETE", expect_json=False
                    )
                )
            self._record(db, user_id, external_id, plan_id, None, None)
            return PlannedWorkoutPushResult(
                provider="wahoo",
                status="created",
                external_id=external_id,
                provider_plan_id=plan_id,
            )

        # 3. Scheduled: move/update the same workout; create it only if there is none (or it is gone).
        workout_form = {
            "workout[plan_id]": int(plan_id) if plan_id.isdigit() else plan_id,
            "workout[starts]": request.scheduled_at.isoformat(),
            "workout[workout_type_id]": payload["workout_type_id"],
            "workout[name]": request.workout.name,
            "workout[minutes]": payload["minutes"],
            "workout[workout_token]": external_id,
        }
        try:
            if workout_id is not None and self._gone(
                lambda: self._make_api_request(
                    db, user_id, f"/v1/workouts/{workout_id}", method="PUT", form_data=workout_form
                )
            ):
                workout_id = None
            if workout_id is None:
                workout_response = self._make_api_request(db, user_id, "/v1/workouts", form_data=workout_form)
                new_workout = workout_response.get("id") if isinstance(workout_response, dict) else None
                workout_id = str(new_workout) if new_workout is not None else None
        except HTTPException as exc:
            self._record(db, user_id, external_id, plan_id, workout_id, scheduled_before)
            if exc.status_code == 401:
                raise
            # The plan exists in the athlete's library: surface its id so a retry can reuse it.
            raise PlannedWorkoutDeliveryError(
                "wahoo", f"plan saved but scheduling failed: {exc.detail}", provider_plan_id=plan_id
            ) from exc
        self._record(db, user_id, external_id, plan_id, workout_id, request.scheduled_at)
        return PlannedWorkoutPushResult(
            provider="wahoo",
            status="scheduled",
            external_id=external_id,
            provider_plan_id=plan_id,
            provider_workout_id=workout_id,
            scheduled_at=request.scheduled_at,
        )

    def remove(self, db: DbSession, user_id: UUID, existing: PlannedWorkoutPush) -> None:
        """Take the workout off the calendar, then drop the plan from the library (404 = already gone)."""
        if existing.provider_workout_id is not None:
            workout_id = existing.provider_workout_id
            self._gone(
                lambda: self._make_api_request(
                    db, user_id, f"/v1/workouts/{workout_id}", method="DELETE", expect_json=False
                )
            )
        if existing.provider_plan_id is not None:
            plan_id = existing.provider_plan_id
            self._gone(
                lambda: self._make_api_request(db, user_id, f"/v1/plans/{plan_id}", method="DELETE", expect_json=False)
            )
