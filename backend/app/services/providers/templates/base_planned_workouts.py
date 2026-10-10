"""Template for pushing planned (structured) workouts to a provider.

Open Wearables is read-first; this is the optional *write* capability. A provider opts in by
setting ``strategy.planned_workouts`` to a subclass of ``BasePlannedWorkoutsTemplate``.

Every implementation follows the same two steps, kept separate so translators stay pure and
unit-testable without HTTP:

1. ``translate()`` – canonical ``PlannedWorkout`` → provider payload (no I/O).
2. ``send()`` – deliver the payload with the user's OAuth token and report what was created.
"""

import logging
from abc import ABC, abstractmethod
from typing import Any
from uuid import UUID, uuid4

from app.database import DbSession
from app.repositories.user_connection_repository import UserConnectionRepository
from app.schemas.planned_workouts import PlannedWorkout, PlannedWorkoutPushRequest, PlannedWorkoutPushResult
from app.services.providers.api_client import make_authenticated_request
from app.services.providers.templates.base_oauth import BaseOAuthTemplate


class PlannedWorkoutTranslationError(ValueError):
    """The canonical workout cannot be expressed in this provider's format (HTTP 422)."""

    def __init__(self, provider: str, reason: str):
        self.detail = f"{provider}: {reason}"
        super().__init__(self.detail)


class PlannedWorkoutDeliveryError(RuntimeError):
    """The provider did not complete the push (HTTP 502).

    ``provider_plan_id`` is set when part of the push already exists on the provider side
    (e.g. the plan was created but scheduling failed), so the caller can reconcile it
    instead of creating a duplicate on retry.
    """

    def __init__(self, provider: str, reason: str, provider_plan_id: str | None = None):
        self.provider_plan_id = provider_plan_id
        self.detail = f"{provider}: {reason}"
        super().__init__(self.detail)


class BasePlannedWorkoutsTemplate(ABC):
    """Base template for the planned-workout push capability."""

    def __init__(
        self,
        connection_repo: UserConnectionRepository,
        provider_name: str,
        api_base_url: str,
        oauth: BaseOAuthTemplate,
    ):
        self.connection_repo = connection_repo
        self.provider_name = provider_name
        self.api_base_url = api_base_url
        self.oauth = oauth
        self.logger = logging.getLogger(self.__class__.__name__)

    @abstractmethod
    def translate(self, workout: PlannedWorkout, request: PlannedWorkoutPushRequest) -> Any:
        """Pure translation canonical → provider payload.

        Raises:
            PlannedWorkoutTranslationError: when the workout uses something the provider cannot express.
        """

    @abstractmethod
    def send(
        self,
        db: DbSession,
        user_id: UUID,
        payload: Any,
        request: PlannedWorkoutPushRequest,
        external_id: str,
    ) -> PlannedWorkoutPushResult:
        """Deliver an already-translated payload for ``user_id``."""

    def push(self, db: DbSession, user_id: UUID, request: PlannedWorkoutPushRequest) -> PlannedWorkoutPushResult:
        """Translate then send. The single entry point used by the API route."""
        external_id = request.external_id or str(uuid4())
        payload = self.translate(request.workout, request)
        return self.send(db, user_id, payload, request, external_id)

    def _make_api_request(
        self,
        db: DbSession,
        user_id: UUID,
        endpoint: str,
        method: str = "POST",
        json_data: dict[str, Any] | None = None,
        form_data: dict[str, Any] | None = None,
    ) -> Any:
        return make_authenticated_request(
            db=db,
            user_id=user_id,
            connection_repo=self.connection_repo,
            oauth=self.oauth,
            api_base_url=self.api_base_url,
            provider_name=self.provider_name,
            endpoint=endpoint,
            method=method,
            json_data=json_data,
            form_data=form_data,
        )
