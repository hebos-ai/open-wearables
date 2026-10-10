"""POST /api/v1/providers/{provider}/users/{user_id}/planned-workouts"""

from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.schemas.planned_workouts import PlannedWorkoutPushResult
from app.services.providers.templates.base_planned_workouts import PlannedWorkoutTranslationError
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
