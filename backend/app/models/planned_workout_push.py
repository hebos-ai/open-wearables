from datetime import datetime
from uuid import UUID

from sqlalchemy import Index
from sqlalchemy.orm import Mapped

from app.database import BaseDbModel
from app.mappings import FKUser, PrimaryKey, str_64, str_100


class PlannedWorkoutPush(BaseDbModel):
    """What a planned workout pushed with an ``external_id`` became on the provider.

    One row per (user, provider, external_id): re-sending the same ``external_id`` updates the
    provider objects recorded here instead of creating new ones, and deleting removes them.
    """

    __tablename__ = "planned_workout_push"
    __table_args__ = (
        Index("ix_planned_workout_push_user_provider_ext", "user_id", "provider", "external_id", unique=True),
    )

    id: Mapped[PrimaryKey[UUID]]
    user_id: Mapped[FKUser]
    provider: Mapped[str_64]
    external_id: Mapped[str_100]
    provider_plan_id: Mapped[str | None]
    provider_workout_id: Mapped[str | None]
    scheduled_at: Mapped[datetime | None]
    updated_at: Mapped[datetime]
