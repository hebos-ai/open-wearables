"""Hebos: workouts from the mobile SDK with their per-sample data.

- the Health Connect / Samsung route (GPS) and Samsung's exercise log become series,
  and the workout stream returns them (coordinates with ~0.1 m precision)
- an Apple workout without a speed series gets speed and distance from its distance
  increments
- ``workout.created`` carries the device's own id (``external_id``) and ``origin: "sdk"``
- a backend with an API key can mint SDK user tokens (no app secret on the backend,
  never anything on the phone)
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy.orm import Session

from app.config import settings
from app.models import DataPointSeries, DataSource, EventRecord
from app.schemas.enums import SeriesType, get_series_type_id
from app.services.apple.healthkit.import_service import ImportService
from app.services.outgoing_webhooks.events import on_workout_created
from app.services.workout_stream_service import distance_increments_to_points, workout_stream_service
from tests.factories import ApiKeyFactory, UserFactory

START = datetime(2026, 10, 31, 7, 0, tzinfo=timezone.utc)
HC_SOURCE = {"appId": "com.google.android.apps.fitness", "deviceModel": "Pixel Watch 2", "deviceType": "watch"}


@pytest.fixture(autouse=True)
def mock_sleep_redis() -> Any:
    mock_redis = MagicMock()
    mock_redis.get.return_value = None
    with (
        patch("app.services.apple.healthkit.sleep_service.get_redis_client", return_value=mock_redis),
        patch("app.integrations.celery.tasks.finalize_stale_sleep_task.finalize_stale_sleeps"),
    ):
        yield mock_redis


@pytest.fixture
def import_service() -> ImportService:
    return ImportService(log=logging.getLogger("test"))


def _iso(seconds: float) -> str:
    return (START + timedelta(seconds=seconds)).isoformat()


def _workout(provider: str, **extra: Any) -> dict[str, Any]:
    return {
        "provider": provider,
        "sdkVersion": "1.0.0",
        "syncTimestamp": "2026-10-31T09:00:00Z",
        "data": {
            "workouts": [
                {
                    "id": "hc-session-1",
                    "type": "running",
                    "startDate": _iso(0),
                    "endDate": _iso(60),
                    "zoneOffset": "+01:00",
                    "source": HC_SOURCE,
                    "values": [{"type": "duration", "value": 60, "unit": "s"}],
                    **extra,
                }
            ]
        },
    }


def _series(db: Session, user_id: UUID, series_type: SeriesType) -> list[DataPointSeries]:
    return (
        db.query(DataPointSeries)
        .join(DataSource, DataPointSeries.data_source_id == DataSource.id)
        .filter(
            DataSource.user_id == user_id,
            DataPointSeries.series_type_definition_id == get_series_type_id(series_type),
        )
        .order_by(DataPointSeries.recorded_at)
        .all()
    )


def _the_workout(db: Session, user_id: UUID) -> EventRecord:
    return (
        db.query(EventRecord)
        .join(DataSource, EventRecord.data_source_id == DataSource.id)
        .filter(DataSource.user_id == user_id, EventRecord.category == "workout")
        .one()
    )


class TestRouteAndLog:
    def test_health_connect_route_becomes_series_and_stream(self, import_service: ImportService, db: Session) -> None:
        user = UserFactory()
        route = [
            {
                "timestamp": _iso(s),
                "latitude": 40.4167754 + s * 1e-5,
                "longitude": -3.7037902,
                "altitudeM": 650.0 + s / 10,
            }
            for s in range(0, 61, 10)
        ]
        # Broken points are skipped, not guessed: no time, out of range.
        route += [{"latitude": 40.4, "longitude": -3.7}, {"timestamp": _iso(5), "latitude": 120, "longitude": 0}]

        import_service.load_data(db, _workout("google", route=route), str(user.id))

        lat = _series(db, user.id, SeriesType.latitude)
        assert len(lat) == 7
        # 6 decimals: a route map needs ~0.1 m, not the ~110 m of 3 decimals.
        assert float(lat[0].value) == pytest.approx(40.416775, abs=1e-6)
        assert len(_series(db, user.id, SeriesType.longitude)) == 7
        assert float(_series(db, user.id, SeriesType.elevation)[-1].value) == pytest.approx(656.0)

        workout = _the_workout(db, user.id)
        stream = workout_stream_service.get_stream(db, user.id, workout.id)
        assert stream is not None
        assert stream["provider"] == "google"
        assert stream["latitude"][0] == pytest.approx(40.416775, abs=1e-6)
        assert stream["elevation"][0] == pytest.approx(650.0)

    def test_samsung_exercise_log_becomes_series(self, import_service: ImportService, db: Session) -> None:
        user = UserFactory()
        samples = [
            {"timestamp": _iso(s), "type": t, "value": v, "unit": u}
            for s in range(0, 61, 5)
            for t, v, u in (("heartRate", 140 + s // 5, "bpm"), ("speed", 3.2, "m/s"), ("cadence", 170, "spm"))
        ]
        samples.append({"timestamp": _iso(5), "type": "unknownThing", "value": 1})

        import_service.load_data(db, _workout("samsung", samples=samples), str(user.id))

        hr = _series(db, user.id, SeriesType.heart_rate)
        assert [int(s.value) for s in hr][:3] == [140, 141, 142]
        assert len(_series(db, user.id, SeriesType.speed)) == 13
        stream = workout_stream_service.get_stream(db, user.id, _the_workout(db, user.id).id)
        assert stream is not None
        assert stream["sample_rate_sec"] == 5
        assert stream["heart_rate"][0] == 140
        assert stream["speed"][0] == pytest.approx(3.2)
        assert stream["cadence"][0] == 170


class TestSpeedFromDistance:
    def test_increments_to_speed_and_cumulative_distance(self) -> None:
        points = distance_increments_to_points([(START, 10.0), (START + timedelta(seconds=5), 15.0)])
        assert (START + timedelta(seconds=5), "distance", 10.0) in points
        assert (START, "speed", 2.0) in points
        # The last increment has no known end: distance only, no speed.
        assert (START + timedelta(seconds=5), "distance", 25.0) in points
        assert sum(1 for p in points if p[1] == "speed") == 1

    def test_no_speed_across_a_pause(self) -> None:
        points = distance_increments_to_points(
            [(START, 10.0), (START + timedelta(seconds=300), 10.0), (START + timedelta(seconds=305), 10.0)]
        )
        speeds = [p for p in points if p[1] == "speed"]
        assert speeds == [(START + timedelta(seconds=300), "speed", 2.0)]

    def test_apple_workout_stream_gets_speed_from_distance(self, import_service: ImportService, db: Session) -> None:
        user = UserFactory()
        payload = _workout("apple")
        payload["data"]["workouts"][0]["source"] = {"name": "Apple Watch", "bundleIdentifier": "com.apple.health"}
        payload["data"]["records"] = [
            {
                "id": f"d{s}",
                "type": "HKQuantityTypeIdentifierDistanceWalkingRunning",
                "value": 15,
                "unit": "m",
                "startDate": _iso(s),
                "endDate": _iso(s + 5),
                "zoneOffset": "+01:00",
                "source": {"name": "Apple Watch", "bundleIdentifier": "com.apple.health"},
            }
            for s in range(0, 56, 5)
        ]

        import_service.load_data(db, payload, str(user.id))

        stream = workout_stream_service.get_stream(db, user.id, _the_workout(db, user.id).id)
        assert stream is not None
        assert stream["speed"][0] == pytest.approx(3.0)
        assert max(v for v in stream["distance"] if v is not None) == pytest.approx(180.0)


class TestWorkoutCreatedIdentity:
    @patch("app.integrations.celery.tasks.emit_webhook_event_task.emit_webhook_event")
    def test_carries_external_id_and_origin(self, mock_task: MagicMock) -> None:
        with patch("app.services.outgoing_webhooks.svix.is_enabled", return_value=True):
            self._emit()
        data = mock_task.delay.call_args[0][1]["data"]
        assert data["external_id"] == "hc-session-1"
        assert data["origin"] == "sdk"

    @staticmethod
    def _emit() -> None:
        on_workout_created(
            record_id=uuid4(),
            user_id=uuid4(),
            provider="google",
            device="Pixel Watch 2",
            workout_type="running",
            start_time=_iso(0),
            end_time=_iso(60),
            zone_offset="+01:00",
            duration_seconds=60,
            external_id="hc-session-1",
            origin="sdk",
        )

    @patch("app.services.event_record_service.svix_service")
    @patch("app.services.event_record_service.on_workout_created")
    def test_sdk_import_says_origin_sdk(
        self, mock_emit: MagicMock, mock_svix: MagicMock, import_service: ImportService, db: Session
    ) -> None:
        mock_svix.is_enabled.return_value = True
        user = UserFactory()
        import_service.load_data(db, _workout("google"), str(user.id))
        db.commit()  # the webhook fires after commit

        assert mock_emit.called
        kwargs = mock_emit.call_args.kwargs
        assert kwargs["external_id"] == "hc-session-1"
        assert kwargs["origin"] == "sdk"


class TestTokenWithApiKey:
    def test_api_key_mints_a_user_token(self, client: TestClient, db: Session, api_v1_prefix: str) -> None:
        user = UserFactory()
        api_key = ApiKeyFactory()

        response = client.post(
            f"{api_v1_prefix}/users/{user.id}/token",
            headers={"X-Open-Wearables-API-Key": api_key.id},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["refresh_token"].startswith("rt-")
        claims = jwt.decode(body["access_token"], settings.secret_key, algorithms=[settings.algorithm])
        assert claims["scope"] == "sdk"
        assert claims["sub"] == str(user.id)
        # The key itself never goes into a token that ends up on a phone.
        assert api_key.id not in body["access_token"]
        assert claims["app_id"].startswith("apikey:")
        assert api_key.id not in claims["app_id"]

    def test_wrong_api_key_is_401(self, client: TestClient, db: Session, api_v1_prefix: str) -> None:
        user = UserFactory()
        response = client.post(
            f"{api_v1_prefix}/users/{user.id}/token",
            headers={"X-Open-Wearables-API-Key": "sk-not-a-key"},
        )
        assert response.status_code == 401
