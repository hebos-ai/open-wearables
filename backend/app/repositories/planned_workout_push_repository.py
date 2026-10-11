"""Where each planned workout pushed with an ``external_id`` lives on the provider.

Re-sending the same ``external_id`` must REPLACE the provider objects, never create new ones,
so every push/delete for one (user, provider, external_id) runs under a Postgres advisory lock
and reads/writes the row kept here. Rows are committed as soon as a provider object exists,
so a later failure (and the request rollback) never loses track of it.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import Connection, Engine, select, text
from sqlalchemy.exc import DBAPIError

from app.database import DbSession
from app.models import PlannedWorkoutPush

logger = logging.getLogger(__name__)

# How long a push/delete waits for another one of the same external_id before giving up.
LOCK_WAIT_SECONDS = 60


class PlannedWorkoutBusyError(RuntimeError):
    """Another push/delete of the same external_id is still running (retry later; HTTP 503)."""


class PlannedWorkoutPushRepository:
    model = PlannedWorkoutPush

    @contextmanager
    def lock(
        self,
        db: DbSession,
        user_id: UUID,
        provider: str,
        external_id: str,
        wait_seconds: float = LOCK_WAIT_SECONDS,
    ) -> Iterator[None]:
        """Serialize pushes/deletes of one external_id across workers.

        Session-level advisory lock on a dedicated AUTOCOMMIT connection: the request session
        commits several times in between (which would release a transaction-level lock), and an
        autocommit connection never sits "idle in transaction" while the provider is called.
        Waiting is bounded by ``lock_timeout``; past it, ``PlannedWorkoutBusyError``.
        """
        bind = db.get_bind()
        engine: Engine = bind.engine if isinstance(bind, Connection) else bind
        key = f"planned_workout_push:{user_id}:{provider}:{external_id}"
        conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
        try:
            conn.execute(text(f"SET lock_timeout = '{int(wait_seconds * 1000)}ms'"))
            try:
                conn.execute(text("SELECT pg_advisory_lock(hashtextextended(:k, 0))"), {"k": key})
            except DBAPIError as exc:
                raise PlannedWorkoutBusyError(f"another push/delete of '{external_id}' is in progress") from exc
            try:
                yield
            finally:
                try:
                    conn.execute(text("SELECT pg_advisory_unlock(hashtextextended(:k, 0))"), {"k": key})
                except DBAPIError:
                    # Never let the unlock hide the operation's result. Dropping the connection
                    # makes Postgres release the lock; it must not go back to the pool holding it.
                    logger.warning("advisory unlock failed for %s; invalidating connection", key, exc_info=True)
                    conn.invalidate()
        finally:
            if not conn.invalidated:
                try:
                    conn.execute(text("RESET lock_timeout"))
                except DBAPIError:
                    conn.invalidate()
            conn.close()

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
