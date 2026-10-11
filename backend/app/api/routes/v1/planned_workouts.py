from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Path, status

from app.database import DbSession
from app.repositories.planned_workout_push_repository import PlannedWorkoutBusyError
from app.schemas.enums import ProviderName
from app.schemas.planned_workouts import (
    PlannedWorkoutDeleteResult,
    PlannedWorkoutPushRequest,
    PlannedWorkoutPushResult,
)
from app.services import ApiKeyDep
from app.services.providers.factory import ProviderFactory
from app.services.providers.templates.base_planned_workouts import (
    PlannedWorkoutDeliveryError,
    PlannedWorkoutTranslationError,
)
from app.utils.exceptions import UnsupportedProviderError

router = APIRouter()
factory = ProviderFactory()


@router.post(
    "/{provider}/users/{user_id}/planned-workouts",
    status_code=status.HTTP_201_CREATED,
)
def push_planned_workout(
    provider: Annotated[ProviderName, Path(description="Provider that will receive the workout")],
    user_id: UUID,
    payload: PlannedWorkoutPushRequest,
    db: DbSession,
    _api_key: ApiKeyDep,
) -> PlannedWorkoutPushResult:
    """Send a planned (structured) workout to the user's connected provider account.

    The workout uses the provider-agnostic canonical format; each provider translates it.
    With `scheduled_at` the workout is put on the athlete's calendar/device for that date,
    otherwise it is only added to their workout library (where the provider has one).

    - **501**: the provider does not support planned workouts
    - **422**: the workout cannot be expressed in the provider's format (e.g. unsupported sport)
    - **401**: the user is not connected to the provider or must re-authorize
    - **502**: the provider did not complete the push (any provider error other than 401);
      `provider_plan_id` is included when the plan exists but could not be scheduled.
      Re-send with the same `external_id`: it updates that plan instead of creating another one
    - **503**: another push/delete of the same `external_id` is still running; retry later

    Re-sending the same `external_id` REPLACES the workout (content and date) on the provider
    instead of creating another one.

    The API key is a backend credential: this endpoint writes to the athlete's provider account,
    so never expose the key to end-user clients.
    """
    strategy = factory.get_provider(provider.value)
    if not strategy.supports_planned_workouts or strategy.planned_workouts is None:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=f"Provider '{provider.value}' does not support planned workouts",
        )

    try:
        return strategy.planned_workouts.push(db, user_id, payload)
    except PlannedWorkoutTranslationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.detail) from exc
    except PlannedWorkoutDeliveryError as exc:
        detail: dict[str, str | None] = {"message": exc.detail, "provider_plan_id": exc.provider_plan_id}
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail) from exc
    except UnsupportedProviderError as exc:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=exc.detail) from exc
    except PlannedWorkoutBusyError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.delete("/{provider}/users/{user_id}/planned-workouts/{external_id}")
def delete_planned_workout(
    provider: Annotated[ProviderName, Path(description="Provider the workout was sent to")],
    user_id: UUID,
    external_id: Annotated[str, Path(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._:-]+$")],
    db: DbSession,
    _api_key: ApiKeyDep,
) -> PlannedWorkoutDeleteResult:
    """Remove a workout pushed with `external_id` from the athlete's provider account (calendar and library).

    Idempotent: an `external_id` with nothing on record returns 200 with `deleted: false`.

    - **501**: the provider does not support planned workouts
    - **401**: the user is not connected to the provider or must re-authorize
    - **502**: the provider failed (any error other than 401/404); the record is kept, retry the delete
    - **503**: another push/delete of the same `external_id` is still running; retry later
    """
    strategy = factory.get_provider(provider.value)
    if not strategy.supports_planned_workouts or strategy.planned_workouts is None:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=f"Provider '{provider.value}' does not support planned workouts",
        )
    try:
        return strategy.planned_workouts.delete(db, user_id, external_id)
    except PlannedWorkoutDeliveryError as exc:
        detail: dict[str, str | None] = {"message": exc.detail, "provider_plan_id": exc.provider_plan_id}
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail) from exc
    except UnsupportedProviderError as exc:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=exc.detail) from exc
    except PlannedWorkoutBusyError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
