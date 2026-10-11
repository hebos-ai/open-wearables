"""Where each planned workout pushed with an ``external_id`` lives on the provider.

Re-sending the same ``external_id`` must REPLACE the provider objects, never create new ones,
so every push/delete for one (user, provider, external_id) runs under a Postgres advisory lock
and reads/writes the row kept here. Rows are committed as soon as a provider object exists,
so a later failure (and the request rollback) never loses track of it.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import Connection, Engine, select, text

from app.database import DbSession
from app.models import PlannedWorkoutPush


class PlannedWorkoutPushRepository:
    model = PlannedWorkoutPush

    @contextmanager
    def lock(self, db: DbSession, user_id: UUID, provider: str, external_id: str) -> Iterator[None]:
        """Serialize pushes/deletes of one external_id across workers.

        Session-level advisory lock on a dedicated connection: the request session commits
        several times in between, which would release a transaction-level lock.
        """
        bind = db.get_bind()
        engine: Engine = bind.engine if isinstance(bind, Connection) else bind
        key = f"planned_workout_push:{user_id}:{provider}:{external_id}"
        with engine.connect() as conn:
            conn.execute(text("SELECT pg_advisory_lock(hashtextextended(:k, 0))"), {"k": key})
            try:
                yield
            finally:
                conn.execute(text("SELECT pg_advisory_unlock(hashtextextended(:k, 0))"), {"k": key})
                conn.commit()

    def get(self, db: DbSession, user_id: UUID, provider: str, external_id: str) -> PlannedWorkoutPush | None:
        stmt = select(self.model).where(
            self.model.user_id == user_id,
            self.model.provider == provider,
            self.model.external_id == external_id,
        )
        return db.execute(stmt).scalar_one_or_none()

    def save(
        self,
        db: DbSession,
        user_id: UUID,
        provider: str,
        external_id: str,
        provider_plan_id: str | None,
        provider_workout_id: str | None,
        scheduled_at: datetime | None,
    ) -> PlannedWorkoutPush:
        """Create or update the row and COMMIT it (provider objects already exist)."""
        row = self.get(db, user_id, provider, external_id)
        if row is None:
            row = self.model(
                id=uuid4(),
                user_id=user_id,
                provider=provider,
                external_id=external_id,
            )
            db.add(row)
        row.provider_plan_id = provider_plan_id
        row.provider_workout_id = provider_workout_id
        row.scheduled_at = scheduled_at
        row.updated_at = datetime.now(timezone.utc)
        db.commit()
        return row

    def delete(self, db: DbSession, row: PlannedWorkoutPush) -> None:
        db.delete(row)
        db.commit()
