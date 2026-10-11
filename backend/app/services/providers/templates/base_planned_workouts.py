"""Template for pushing planned (structured) workouts to a provider.

Open Wearables is read-first; this is the optional *write* capability. A provider opts in by
setting ``strategy.planned_workouts`` to a subclass of ``BasePlannedWorkoutsTemplate``.

Every implementation follows the same two steps, kept separate so translators stay pure and
unit-testable without HTTP:

1. ``translate()`` – canonical ``PlannedWorkout`` → provider payload (no I/O).
2. ``send()`` – deliver the payload with the user's OAuth token and report what was created.

Idempotency: a push is identified by its ``external_id``. ``push()`` looks up what a previous push
with the same id created on the provider (``PlannedWorkoutPush`` row) and hands it to ``send()``,
which must UPDATE those objects instead of creating new ones. ``delete()`` removes them through
``remove()``. Both run under a per-(user, provider, external_id) lock.
"""

import logging
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from app.database import DbSession
from app.models import PlannedWorkoutPush
from app.repositories.planned_workout_push_repository import PlannedWorkoutPushRepository
from app.repositories.user_connection_repository import UserConnectionRepository
from app.schemas.planned_workouts import (
    PlannedWorkout,
    PlannedWorkoutDeleteResult,
    PlannedWorkoutPushRequest,
    PlannedWorkoutPushResult,
)
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
        self.pushes = PlannedWorkoutPushRepository()
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
        existing: PlannedWorkoutPush | None,
    ) -> PlannedWorkoutPushResult:
        """Deliver an already-translated payload for ``user_id``.

        ``existing`` is what an earlier push with the same ``external_id`` created: update those
        provider objects (re-create only what the provider no longer has). Record every provider
        object as soon as it exists with ``self._record(...)``.
        """

    @abstractmethod
    def remove(self, db: DbSession, user_id: UUID, existing: PlannedWorkoutPush) -> None:
        """Remove from the provider what ``existing`` points to (already-gone objects are fine)."""

    def push(self, db: DbSession, user_id: UUID, request: PlannedWorkoutPushRequest) -> PlannedWorkoutPushResult:
        """Translate then send. The single entry point used by the API route."""
        external_id = request.external_id or str(uuid4())
        payload = self.translate(request.workout, request)
        with self.pushes.lock(db, user_id, self.provider_name, external_id):
            existing = self.pushes.get(db, user_id, self.provider_name, external_id)
            return self.send(db, user_id, payload, request, external_id, existing)

    def delete(self, db: DbSession, user_id: UUID, external_id: str) -> PlannedWorkoutDeleteResult:
        """Remove the workout pushed with ``external_id``. Idempotent: unknown ids return ``deleted=False``."""
        with self.pushes.lock(db, user_id, self.provider_name, external_id):
            existing = self.pushes.get(db, user_id, self.provider_name, external_id)
            if existing is None:
                return PlannedWorkoutDeleteResult(provider=self.provider_name, external_id=external_id, deleted=False)
            self.remove(db, user_id, existing)
            self.pushes.delete(db, existing)
            return PlannedWorkoutDeleteResult(provider=self.provider_name, external_id=external_id, deleted=True)

    def _record(
        self,
        db: DbSession,
        user_id: UUID,
        external_id: str,
        provider_plan_id: str | None,
        provider_workout_id: str | None,
        scheduled_at: datetime | None,
    ) -> None:
        """Persist (and commit) what exists on the provider for this ``external_id``."""
        self.pushes.save(
            db, user_id, self.provider_name, external_id, provider_plan_id, provider_workout_id, scheduled_at
        )

    def _make_api_request(
        self,
        db: DbSession,
        user_id: UUID,
        endpoint: str,
        method: str = "POST",
        json_data: dict[str, Any] | None = None,
        form_data: dict[str, Any] | None = None,
        expect_json: bool = True,
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
            expect_json=expect_json,
        )
