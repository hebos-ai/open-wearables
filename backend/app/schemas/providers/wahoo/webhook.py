"""Wahoo webhook payload.

Wahoo POSTs a full ``workout_summary`` (with the parent ``workout`` nested inside) to the
webhook URL configured in the developer portal, echoing the portal's webhook token in the
body. Delivery requires the user's token to carry the ``offline_data`` scope.

Source: https://cloud-api.wahooligan.com/#webhooks
"""

from pydantic import BaseModel, ConfigDict

from app.schemas.providers.wahoo.workout_import import WahooWorkoutSummaryJSON


class WahooWebhookUser(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: int


class WahooWebhookWorkoutRef(BaseModel):
    """``workout`` object nested in a webhook ``workout_summary`` (no summary of its own)."""

    model_config = ConfigDict(extra="allow")

    id: int
    starts: str | None = None
    minutes: int | None = None
    name: str | None = None
    plan_id: int | None = None
    workout_token: str | None = None
    workout_type_id: int | None = None


class WahooWebhookSummary(WahooWorkoutSummaryJSON):
    workout: WahooWebhookWorkoutRef | None = None


class WahooWebhookEvent(BaseModel):
    model_config = ConfigDict(extra="allow")

    event_type: str
    webhook_token: str | None = None
    user: WahooWebhookUser
    workout_summary: WahooWebhookSummary | None = None
