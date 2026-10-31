"""
Tests for GET /api/v1/users/{user_id}/events/workouts/{workout_id}/stream.

- stored samples inside the workout window come back on a regular grid
- samples outside the window, or of another user, are not included
- Polar exercises are read on demand from AccessLink (samples + route)
- 404 when the workout is someone else's or has no samples
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.schemas.enums import ProviderName
from app.services.workout_stream_service import polar_exercise_to_stream, series_to_stream
from tests.factories import (
    ApiKeyFactory,
    DataPointSeriesFactory,
    DataSourceFactory,
    EventRecordFactory,
    SeriesTypeDefinitionFactory,
    UserFactory,
)
from tests.utils import api_key_headers

START = datetime(2026, 10, 10, 7, 0, tzinfo=timezone.utc)


class TestWorkoutStreamEndpoint:
    def test_stored_heart_rate_on_a_regular_grid(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        ds = DataSourceFactory(user=user, provider=ProviderName.APPLE)
        workout = EventRecordFactory(
            mapping=ds, category="workout", type_="running", start_datetime=START, duration_seconds=60
        )
        hr = SeriesTypeDefinitionFactory.get_or_create_heart_rate()
        for s in range(0, 61, 5):
            DataPointSeriesFactory(
                data_source=ds, series_type=hr, recorded_at=START + timedelta(seconds=s), value=120 + s
            )
        # Before the workout: not part of it.
        DataPointSeriesFactory(data_source=ds, series_type=hr, recorded_at=START - timedelta(seconds=30), value=60)
        api_key = ApiKeyFactory()

        response = client.get(
            f"/api/v1/users/{user.id}/events/workouts/{workout.id}/stream",
            headers=api_key_headers(api_key.id),
        )

        assert response.status_code == 200
        body = response.json()
        assert body["provider"] == "apple"
        assert body["sample_rate_sec"] == 5
        assert body["t"][:3] == [0, 5, 10]
        assert body["heart_rate"][0] == 120
        assert body["heart_rate"][-1] == 180
        assert 60 not in body["heart_rate"]
        assert body["power"] is None
        assert body["latitude"] is None

    def test_polar_reads_samples_and_route_on_demand(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        ds = DataSourceFactory(user=user, provider=ProviderName.POLAR, device_model="Polar Vantage V3")
        workout = EventRecordFactory(
            mapping=ds,
            category="workout",
            type_="cycling",
            start_datetime=START,
            duration_seconds=4,
            external_id="polar-ex-1",
        )
        exercise = {
            "id": "polar-ex-1",
            "samples": [
                {"recording-rate": 1, "sample-type": "0", "data": "100,101,102,103,104"},
                {"recording-rate": 2, "sample-type": "4", "data": "200,210,220"},
                {"recording-rate": 1, "sample-type": "1", "data": "36,36,,36,36"},
            ],
            "route": [
                {"latitude": 60.1, "longitude": 24.9, "time": "PT0S"},
                {"latitude": 60.2, "longitude": 25.0, "time": "PT2S"},
            ],
        }
        api_key = ApiKeyFactory()

        with patch(
            "app.services.providers.polar.workouts.PolarWorkouts.get_exercise_detail", return_value=exercise
        ) as detail:
            response = client.get(
                f"/api/v1/users/{user.id}/events/workouts/{workout.id}/stream",
                headers=api_key_headers(api_key.id),
            )

        assert response.status_code == 200
        detail.assert_called_once()
        assert detail.call_args.args[2] == "polar-ex-1"
        body = response.json()
        assert body["provider"] == "polar"
        assert body["sample_rate_sec"] == 1
        assert body["heart_rate"] == [100, 101, 102, 103, 104]
        assert body["power"] == [200, None, 210, None, 220]
        assert body["speed"] == [10, 10, None, 10, 10]
        assert body["latitude"] == [60.1, None, 60.2, None, None]

    def test_other_users_workout_is_404(self, client: TestClient, db: Session) -> None:
        owner = UserFactory()
        ds = DataSourceFactory(user=owner)
        workout = EventRecordFactory(mapping=ds, category="workout", start_datetime=START, duration_seconds=60)
        DataPointSeriesFactory(data_source=ds, recorded_at=START + timedelta(seconds=10), value=130)
        api_key = ApiKeyFactory()

        response = client.get(
            f"/api/v1/users/{UserFactory().id}/events/workouts/{workout.id}/stream",
            headers=api_key_headers(api_key.id),
        )

        assert response.status_code == 404

    def test_workout_without_samples_is_404(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        ds = DataSourceFactory(user=user, provider=ProviderName.WAHOO)
        workout = EventRecordFactory(mapping=ds, category="workout", start_datetime=START, duration_seconds=60)
        api_key = ApiKeyFactory()

        response = client.get(
            f"/api/v1/users/{user.id}/events/workouts/{uuid4()}/stream",
            headers=api_key_headers(api_key.id),
        )
        assert response.status_code == 404

        response = client.get(
            f"/api/v1/users/{user.id}/events/workouts/{workout.id}/stream",
            headers=api_key_headers(api_key.id),
        )
        assert response.status_code == 404


class TestStreamBuilders:
    def test_series_without_points_is_none(self) -> None:
        assert series_to_stream([], START, START + timedelta(seconds=10)) is None

    def test_series_keeps_gaps_as_none(self) -> None:
        points = [
            (START, "heart_rate", 100.0),
            (START + timedelta(seconds=1), "heart_rate", 101.0),
            (START + timedelta(seconds=3), "heart_rate", 103.0),
            (START + timedelta(seconds=1), "power", 250.0),
        ]
        out = series_to_stream(points, START, START + timedelta(seconds=3))
        assert out is not None
        assert out["sample_rate_sec"] == 1
        assert out["heart_rate"] == [100.0, 101.0, None, 103.0]
        assert out["power"] == [None, 250.0, None, None]
        assert out["cadence"] is None

    def test_polar_route_with_timestamps(self) -> None:
        exercise = {
            "samples": [{"recording-rate": 1, "sample-type": "0", "data": "90,91,92"}],
            "route": [
                {"latitude": 1.0, "longitude": 2.0, "time": "2026-10-10T07:00:00.000Z"},
                {"latitude": 1.5, "longitude": 2.5, "time": "2026-10-10T07:00:02.000Z"},
            ],
        }
        out = polar_exercise_to_stream(exercise, 2)
        assert out is not None
        assert out["latitude"] == [1.0, None, 1.5]
        assert out["longitude"] == [2.0, None, 2.5]

    def test_polar_without_samples_or_route_is_none(self) -> None:
        assert polar_exercise_to_stream({"samples": [], "route": []}, 60) is None
