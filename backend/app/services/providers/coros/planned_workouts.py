from typing import Any, NoReturn
from uuid import UUID

from app.database import DbSession
from app.models import PlannedWorkoutPush
from app.schemas.planned_workouts import PlannedWorkout, PlannedWorkoutPushRequest, PlannedWorkoutPushResult
from app.services.providers.coros.pending import pending_partner_access
from app.services.providers.templates.base_planned_workouts import BasePlannedWorkoutsTemplate


class CorosPlannedWorkouts(BasePlannedWorkoutsTemplate):
    """Planned-workout push for COROS.

    COROS states that partners can push structured workouts and training plans, but the
    workout format is only documented under partner access. Implement ``translate`` (pure,
    canonical → COROS) and ``send`` against that documentation; the route, request model and
    error handling are already shared with the other providers.
    """

    def translate(self, workout: PlannedWorkout, request: PlannedWorkoutPushRequest) -> NoReturn:
        raise pending_partner_access("planned workout push")

    def send(
        self,
        db: DbSession,
        user_id: UUID,
        payload: Any,
        request: PlannedWorkoutPushRequest,
        external_id: str,
        existing: PlannedWorkoutPush | None,
    ) -> PlannedWorkoutPushResult:
        raise pending_partner_access("planned workout push")

    def remove(self, db: DbSession, user_id: UUID, existing: PlannedWorkoutPush) -> NoReturn:
        raise pending_partner_access("planned workout delete")
