"""Wahoo webhook handler.

Wahoo pushes the full ``workout_summary`` (with its parent ``workout`` nested inside) to the
URL configured in the developer portal: ``{API_BASE_URL}/api/v1/providers/wahoo/webhooks``.

Verification
------------
Wahoo does not sign requests. Instead it echoes the portal's *Webhook Token* in the JSON
body (``webhook_token``); it is compared in constant time with ``WAHOO_WEBHOOK_TOKEN``.
With no token configured every request is rejected. The token is stripped before the raw
payload is stored.

Delivery model
--------------
Wahoo retries non-200 deliveries (30 min, 4 h, 24 h, 72 h). ``dispatch()`` stores the payload
and enqueues the shared ``process_webhook_push`` task, so the response is immediate;
``process_payload()`` maps ``user.id`` to the connection and saves the workout.

See: https://cloud-api.wahooligan.com/#webhooks
"""

import json
import logging
from typing import Any
from uuid import uuid4

from celery import current_app as celery_app
from fastapi import HTTPException, Request
from pydantic import ValidationError

from app.config import settings
from app.database import DbSession
from app.repositories import UserConnectionRepository
from app.schemas.providers.wahoo import WahooWebhookEvent, WahooWorkoutJSON
from app.services.providers.templates.base_webhook_handler import BaseWebhookHandler
from app.services.providers.wahoo.workouts import WahooWorkouts
from app.services.raw_payload_storage import store_raw_payload
from app.utils.structured_logging import log_structured

logger = logging.getLogger(__name__)

_PROCESS_PUSH_TASK = "app.integrations.celery.tasks.webhook_push_task.process_webhook_push"

WORKOUT_SUMMARY_EVENT = "workout_summary"


class WahooWebhookHandler(BaseWebhookHandler):
    """Webhook handler for Wahoo full-payload ``workout_summary`` events."""

    def __init__(self, workouts: WahooWorkouts) -> None:
        super().__init__("wahoo")
        self.workouts = workouts
        self.connection_repo = UserConnectionRepository()

    def extract_user_id(self, payload: dict[str, Any]) -> str | None:
        user = payload.get("user")
        if isinstance(user, dict) and user.get("id") is not None:
            return str(user["id"])
        return None

    # ------------------------------------------------------------------
    # BaseWebhookHandler interface
    # ------------------------------------------------------------------

    def verify_signature(self, request: Request, body: bytes) -> bool:
        expected = settings.wahoo_webhook_token.get_secret_value() if settings.wahoo_webhook_token else ""
        if not expected:
            log_structured(
                logger,
                "warning",
                "WAHOO_WEBHOOK_TOKEN not configured — rejecting webhook",
                provider="wahoo",
                action="webhook_signature_missing_secret",
            )
            return False
        try:
            provided = json.loads(body).get("webhook_token")
        except (json.JSONDecodeError, ValueError, AttributeError):
            return False
        if not isinstance(provided, str) or not provided:
            return False
        return self._verify_token(expected, provided)

    def parse_payload(self, body: bytes) -> WahooWebhookEvent:
        try:
            return WahooWebhookEvent.model_validate(json.loads(body))
        except (json.JSONDecodeError, ValueError) as exc:
            # pydantic.ValidationError is a ValueError subclass
            raise HTTPException(status_code=400, detail="Invalid Wahoo webhook payload") from exc

    def dispatch(self, db: DbSession, payload: WahooWebhookEvent) -> dict[str, Any]:
        trace_id = str(uuid4())[:8]
        raw = payload.model_dump(mode="json", exclude={"webhook_token"})

        log_structured(
            logger,
            "info",
            "Received Wahoo webhook",
            provider="wahoo",
            trace_id=trace_id,
            event_type=payload.event_type,
            wahoo_user_id=payload.user.id,
        )
        store_raw_payload(source="webhook", provider="wahoo", payload=raw, trace_id=trace_id)

        task = celery_app.send_task(_PROCESS_PUSH_TASK, args=["wahoo", raw, trace_id], queue="webhook_sync")
        log_structured(
            logger,
            "info",
            "Enqueued Wahoo webhook processing task",
            provider="wahoo",
            trace_id=trace_id,
            task_id=getattr(task, "id", None),
        )
        return {"status": "accepted"}

    def supported_event_types(self) -> list[str]:
        return [WORKOUT_SUMMARY_EVENT]

    # ------------------------------------------------------------------
    # Async processing (process_webhook_push task)
    # ------------------------------------------------------------------

    def process_payload(self, db: DbSession, payload: dict[str, Any], trace_id: str) -> dict[str, Any]:
        try:
            event = WahooWebhookEvent.model_validate(payload)
        except ValidationError as exc:
            return {"status": "error", "error": f"Invalid payload: {exc}"}

        if event.event_type != WORKOUT_SUMMARY_EVENT:
            return {"status": "ignored", "reason": f"event_type:{event.event_type}"}

        connection = self.connection_repo.get_by_provider_user_id(db, "wahoo", str(event.user.id))
        if not connection:
            log_structured(
                logger,
                "warning",
                "No connection found for Wahoo user",
                provider="wahoo",
                trace_id=trace_id,
                wahoo_user_id=event.user.id,
            )
            return {"status": "user_not_found", "wahoo_user_id": event.user.id}

        user_id = connection.user_id
        summary = event.workout_summary
        if summary is None or summary.workout is None or not summary.workout.starts:
            return {"status": "ignored", "reason": "missing_workout", "user_id": str(user_id)}

        ref = summary.workout
        workout = WahooWorkoutJSON.model_validate(
            {
                **ref.model_dump(),
                "workout_summary": summary.model_dump(exclude={"workout"}),
            }
        )

        self.connection_repo.update_last_synced_at(db, connection)
        record_id = self.workouts.save_workout(db, user_id, workout)
        log_structured(
            logger,
            "info",
            "Wahoo workout saved",
            provider="wahoo",
            trace_id=trace_id,
            user_id=str(user_id),
            wahoo_workout_id=ref.id,
        )
        return {
            "status": "processed",
            "records_saved": 1 if record_id else 0,
            "workout_id": ref.id,
            "user_id": str(user_id),
        }
