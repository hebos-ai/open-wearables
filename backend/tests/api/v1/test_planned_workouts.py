"""POST /api/v1/providers/{provider}/users/{user_id}/planned-workouts"""

from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.schemas.planned_workouts import PlannedWorkoutPushResult
from app.services.providers.templates.base_planned_workouts import (
    PlannedWorkoutDeliveryError,
    PlannedWorkoutTranslationError,
)
from tests.factories import ApiKeyFactory, UserConnectionFactory, UserFactory

BODY = {
    "workout": {
        "name": "Sweet spot",
        "sport": "bike",
        "steps": [{"kind": "interval", "durationSec": 600, "target": {"type": "power", "low": 230, "high": 245}}],
    },
    "scheduledAt": "2026-10-12T07:00:00+02:00",
    "externalId": "ext-1",
}

PUSH = "app.services.providers.wahoo.planned_workouts.WahooPlannedWorkouts.push"


def _url(provider: str, user_id: object) -> str:
    return f"/api/v1/providers/{provider}/users/{user_id}/planned-workouts"


def test_requires_api_key(client: TestClient, db: Session) -> None:
    response = client.post(_url("wahoo", UserFactory().id), json=BODY)
    assert response.status_code == 401


def test_pushes_to_wahoo(client: TestClient, db: Session) -> None:
    user = UserFactory()
    UserConnectionFactory(user=user, provider="wahoo")
    result = PlannedWorkoutPushResult(
        provider="wahoo", status="scheduled", external_id="ext-1", provider_plan_id="7", provider_workout_id="8"
    )
    with patch(PUSH, return_value=result) as push:
        response = client.post(
            _url("wahoo", user.id), json=BODY, headers={"X-Open-Wearables-API-Key": ApiKeyFactory().id}
        )

    assert response.status_code == 201
    assert response.json()["provider_workout_id"] == "8"
    sent = push.call_args.args[2]
    assert sent.external_id == "ext-1"
    assert sent.workout.steps[0].duration_sec == 600


def test_translation_error_is_422(client: TestClient, db: Session) -> None:
    with patch(PUSH, side_effect=PlannedWorkoutTranslationError("wahoo", "sport 'swim' is not supported")):
        response = client.post(
            _url("wahoo", UserFactory().id), json=BODY, headers={"X-Open-Wearables-API-Key": ApiKeyFactory().id}
        )
    assert response.status_code == 422
    assert "swim" in response.json()["detail"]


def test_invalid_canonical_workout_is_422(client: TestClient, db: Session) -> None:
    body = {"workout": {"name": "x", "sport": "bike", "steps": []}}
    response = client.post(
        _url("wahoo", UserFactory().id), json=body, headers={"X-Open-Wearables-API-Key": ApiKeyFactory().id}
    )
    assert response.status_code in (400, 422)


def test_provider_without_capability_is_501(client: TestClient, db: Session) -> None:
    for provider in ("polar", "coros"):
        response = client.post(
            _url(provider, UserFactory().id), json=BODY, headers={"X-Open-Wearables-API-Key": ApiKeyFactory().id}
        )
        assert response.status_code == 501, provider


def test_user_not_connected_is_401(client: TestClient, db: Session) -> None:
    response = client.post(
        _url("wahoo", UserFactory().id), json=BODY, headers={"X-Open-Wearables-API-Key": ApiKeyFactory().id}
    )
    assert response.status_code == 401


def test_delivery_error_is_502_with_plan_id(client: TestClient, db: Session) -> None:
    error = PlannedWorkoutDeliveryError("wahoo", "plan created but scheduling failed", provider_plan_id="777")
    with patch(PUSH, side_effect=error):
        response = client.post(
            _url("wahoo", UserFactory().id), json=BODY, headers={"X-Open-Wearables-API-Key": ApiKeyFactory().id}
        )
    assert response.status_code == 502
    assert response.json()["detail"]["provider_plan_id"] == "777"


# --- Idempotent re-send and delete, end to end through the API and the database ------------------

SERIES_BODY = {
    "workout": {
        "name": "Series intensas",
        "sport": "run",
        "steps": [
            {"kind": "warmup", "durationSec": 900, "name": "Calentamiento"},
            {
                "kind": "repeat",
                "repetitions": 5,
                "steps": [
                    {"kind": "interval", "durationSec": 210, "target": {"type": "pace", "low": 3.333, "high": 3.6}},
                    {"kind": "recovery", "durationSec": 180, "name": "Trote", "note": "suave"},
                ],
            },
        ],
    },
    "scheduledAt": "2026-10-13T18:30:00+02:00",
    "externalId": "hebos-tce-77",
}
API = "app.services.providers.wahoo.planned_workouts.WahooPlannedWorkouts._make_api_request"


def _delete_url(provider: str, user_id: object, external_id: str) -> str:
    return f"{_url(provider, user_id)}/{external_id}"


def test_resend_replaces_and_delete_removes(client: TestClient, db: Session) -> None:
    from app.models import PlannedWorkoutPush
    from tests.providers.wahoo.test_wahoo_planned_workouts import FakeWahooApi

    user = UserFactory()
    UserConnectionFactory(user=user, provider="wahoo")
    headers = {"X-Open-Wearables-API-Key": ApiKeyFactory().id}
    api = FakeWahooApi()
    with patch(API, side_effect=api):
        r1 = client.post(_url("wahoo", user.id), json=SERIES_BODY, headers=headers)
        moved = {**SERIES_BODY, "scheduledAt": "2026-10-15T18:30:00+02:00"}
        r2 = client.post(_url("wahoo", user.id), json=moved, headers=headers)

    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r2.json()["provider_plan_id"] == r1.json()["provider_plan_id"]
    assert r2.json()["provider_workout_id"] == r1.json()["provider_workout_id"]
    assert [c for c in api.calls if c[0] == "POST"] == [("POST", "/v1/plans"), ("POST", "/v1/workouts")]
    assert len(api.plans) == 1
    assert len(api.workouts) == 1
    rows = db.query(PlannedWorkoutPush).filter_by(user_id=user.id).all()
    assert len(rows) == 1
    assert rows[0].external_id == "hebos-tce-77"
    assert rows[0].scheduled_at is not None
    assert rows[0].scheduled_at.date().isoformat() == "2026-10-15"

    with patch(API, side_effect=api):
        d1 = client.delete(_delete_url("wahoo", user.id, "hebos-tce-77"), headers=headers)
        d2 = client.delete(_delete_url("wahoo", user.id, "hebos-tce-77"), headers=headers)
    assert d1.status_code == 200
    assert d1.json() == {"provider": "wahoo", "external_id": "hebos-tce-77", "deleted": True}
    assert d2.json()["deleted"] is False
    assert api.plans == {}
    assert api.workouts == {}
    assert db.query(PlannedWorkoutPush).filter_by(user_id=user.id).count() == 0


def test_delete_requires_api_key_and_a_capable_provider(client: TestClient, db: Session) -> None:
    user = UserFactory()
    assert client.delete(_delete_url("wahoo", user.id, "x1")).status_code == 401
    headers = {"X-Open-Wearables-API-Key": ApiKeyFactory().id}
    for provider in ("polar", "coros"):
        assert client.delete(_delete_url(provider, user.id, "x1"), headers=headers).status_code == 501, provider
    assert client.delete(_delete_url("wahoo", user.id, "a b"), headers=headers).status_code == 400


def test_delete_of_a_known_workout_when_wahoo_is_disconnected_is_401(client: TestClient, db: Session) -> None:
    from tests.providers.wahoo.test_wahoo_planned_workouts import FakeWahooApi

    user = UserFactory()
    connection = UserConnectionFactory(user=user, provider="wahoo")
    headers = {"X-Open-Wearables-API-Key": ApiKeyFactory().id}
    with patch(API, side_effect=FakeWahooApi()):
        client.post(_url("wahoo", user.id), json=SERIES_BODY, headers=headers)
    db.delete(connection)
    db.flush()
    response = client.delete(_delete_url("wahoo", user.id, "hebos-tce-77"), headers=headers)
    assert response.status_code == 401


def test_delete_provider_failure_is_502_and_keeps_the_record(client: TestClient, db: Session) -> None:
    from fastapi import HTTPException

    from app.models import PlannedWorkoutPush
    from tests.providers.wahoo.test_wahoo_planned_workouts import FakeWahooApi

    user = UserFactory()
    UserConnectionFactory(user=user, provider="wahoo")
    headers = {"X-Open-Wearables-API-Key": ApiKeyFactory().id}
    with patch(API, side_effect=FakeWahooApi()):
        client.post(_url("wahoo", user.id), json=SERIES_BODY, headers=headers)
    with patch(API, side_effect=HTTPException(status_code=500, detail="Wahoo API error: boom")):
        response = client.delete(_delete_url("wahoo", user.id, "hebos-tce-77"), headers=headers)
    assert response.status_code == 502
    assert db.query(PlannedWorkoutPush).filter_by(user_id=user.id).count() == 1


def test_same_external_id_is_serialized_and_a_long_wait_is_503(db: Session) -> None:
    """Real advisory lock: while one push of an external_id runs, another one waits (and gives up → busy)."""
    import threading
    from uuid import uuid4

    import pytest

    from app.repositories.planned_workout_push_repository import (
        PlannedWorkoutBusyError,
        PlannedWorkoutPushRepository,
    )

    repo = PlannedWorkoutPushRepository()
    user_id = uuid4()
    holding = threading.Event()
    release = threading.Event()

    def first() -> None:
        with repo.lock(db, user_id, "wahoo", "hebos-tce-77"):
            holding.set()
            release.wait(5)

    thread = threading.Thread(target=first)
    thread.start()
    try:
        assert holding.wait(5)
        with pytest.raises(PlannedWorkoutBusyError), repo.lock(db, user_id, "wahoo", "hebos-tce-77", wait_seconds=0.2):
            pass
        # Another external_id is not blocked.
        with repo.lock(db, user_id, "wahoo", "hebos-tce-78", wait_seconds=0.2):
            pass
    finally:
        release.set()
        thread.join(5)
    # Once released, the same id can be locked again (the unlock reached Postgres).
    with repo.lock(db, user_id, "wahoo", "hebos-tce-77", wait_seconds=0.2):
        pass


def test_busy_lock_is_503(client: TestClient, db: Session) -> None:
    from app.repositories.planned_workout_push_repository import PlannedWorkoutBusyError

    user = UserFactory()
    headers = {"X-Open-Wearables-API-Key": ApiKeyFactory().id}
    busy = PlannedWorkoutBusyError("another push/delete of 'hebos-tce-77' is in progress")
    with patch("app.repositories.planned_workout_push_repository.PlannedWorkoutPushRepository.lock", side_effect=busy):
        assert client.post(_url("wahoo", user.id), json=SERIES_BODY, headers=headers).status_code == 503
        assert client.delete(_delete_url("wahoo", user.id, "hebos-tce-77"), headers=headers).status_code == 503
