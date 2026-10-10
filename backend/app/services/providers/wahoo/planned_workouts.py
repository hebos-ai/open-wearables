"""Push planned workouts to Wahoo (``POST /v1/plans`` + optional ``POST /v1/workouts``).

Canonical ``PlannedWorkout`` → Wahoo *plan.json* v1.0.0
(https://cloud-api.wahooligan.com/docs/plan-json-format.pdf):

==================  =========================================================
canonical           plan.json
==================  =========================================================
sport               header.workout_type_family (0 biking, 1 running) +
                    header.workout_type_location (0 indoor, 1 outdoor)
step.kind           interval.intensity_type (wu / active / recover / cd / rest)
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
"""

import base64
import json
import math
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import HTTPException

from app.database import DbSession
from app.schemas.planned_workouts import (
    PlannedWorkout,
    PlannedWorkoutPushRequest,
    PlannedWorkoutPushResult,
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
            "name": STEP_NAMES[step.kind],
            "exit_trigger_type": trigger_type,
            "exit_trigger_value": trigger_value,
            "intensity_type": INTENSITY[step.kind],
        }
        if step.target is not None:
            low, high = step.target.bounds
            interval["targets"] = [{"type": TARGET_TYPES[step.target.type], "low": _number(low), "high": _number(high)}]
        return interval

    def translate(self, workout: PlannedWorkout, request: PlannedWorkoutPushRequest) -> dict[str, Any]:
        family, location, workout_type_id = self._sport(workout)
        intervals = [self._interval(i, step) for i, step in enumerate(workout.steps)]

        header: dict[str, Any] = {
            "name": workout.name,
            "version": PLAN_FORMAT_VERSION,
            "workout_type_family": family,
            "workout_type_location": location,
        }
        if request.description:
            header["description"] = request.description
        if all(step.duration_sec is not None for step in workout.steps):
            header["duration_s"] = sum(step.duration_sec or 0 for step in workout.steps)
        if all(step.distance_m is not None for step in workout.steps):
            header["distance_m"] = _number(sum(step.distance_m or 0 for step in workout.steps))

        estimated = sum(_estimated_seconds(step) for step in workout.steps)
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

    def send(
        self,
        db: DbSession,
        user_id: UUID,
        payload: dict[str, Any],
        request: PlannedWorkoutPushRequest,
        external_id: str,
    ) -> PlannedWorkoutPushResult:
        plan_response = self._make_api_request(
            db,
            user_id,
            "/v1/plans",
            form_data={
                "plan[file]": self.encode_plan_file(payload["plan"]),
                "plan[filename]": "plan.json",
                "plan[external_id]": external_id,
                "plan[provider_updated_at]": datetime.now(timezone.utc).isoformat(),
            },
        )
        plan_id = plan_response.get("id") if isinstance(plan_response, dict) else None
        if plan_id is None:
            raise PlannedWorkoutDeliveryError("wahoo", "POST /v1/plans returned no plan id")

        if request.scheduled_at is None:
            return PlannedWorkoutPushResult(
                provider="wahoo",
                status="created",
                external_id=external_id,
                provider_plan_id=str(plan_id),
            )

        try:
            workout_response = self._make_api_request(
                db,
                user_id,
                "/v1/workouts",
                form_data={
                    "workout[plan_id]": plan_id,
                    "workout[starts]": request.scheduled_at.isoformat(),
                    "workout[workout_type_id]": payload["workout_type_id"],
                    "workout[name]": request.workout.name,
                    "workout[minutes]": payload["minutes"],
                    "workout[workout_token]": external_id,
                },
            )
        except HTTPException as exc:
            if exc.status_code == 401:
                raise
            # The plan exists in the athlete's library: surface its id so a retry can reuse it.
            raise PlannedWorkoutDeliveryError(
                "wahoo", f"plan created but scheduling failed: {exc.detail}", provider_plan_id=str(plan_id)
            ) from exc
        workout_id = workout_response.get("id") if isinstance(workout_response, dict) else None
        return PlannedWorkoutPushResult(
            provider="wahoo",
            status="scheduled",
            external_id=external_id,
            provider_plan_id=str(plan_id),
            provider_workout_id=str(workout_id) if workout_id is not None else None,
            scheduled_at=request.scheduled_at,
        )
