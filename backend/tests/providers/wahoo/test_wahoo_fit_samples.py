"""Wahoo: per-sample workout data read from the workout's FIT file (synthetic FIT, no real user data)."""

from collections.abc import Iterator
from unittest.mock import patch
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import DataPointSeries, DataSource, EventRecord
from app.schemas.enums import SeriesType
from app.schemas.enums.series_types import get_series_type_id
from app.schemas.providers.wahoo import WahooWorkoutJSON
from app.services.event_record_service import event_record_service
from app.services.fit_parser import FIT_RECORD_SERIES
from app.services.providers.wahoo.coverage import TIMESERIES as WAHOO_TIMESERIES
from app.services.providers.wahoo.strategy import WahooStrategy
from app.services.workout_stream_service import workout_stream_service
from tests.factories import UserConnectionFactory, UserFactory
from tests.fixtures.fit_builder import make_running_fit
from tests.providers.wahoo.conftest import make_summary, make_workout

CDN_URL = "https://cdn.wahooligan.com/wahoo-cloud/workout_file/synthetic.fit"
# make_running_fit(): 20 records from 2025-06-01 08:00:00 UTC, 1 s apart.
FIT_START = "2025-06-01T08:00:00.000Z"


@pytest.fixture
def samples_on() -> Iterator[None]:
    with patch.object(settings, "ingest_workout_samples", True):
        yield


def _count(db: Session, user_id: UUID, series: SeriesType) -> int:
    q = (
        select(func.count())
        .select_from(DataPointSeries)
        .join(DataSource, DataSource.id == DataPointSeries.data_source_id)
        .where(
            DataSource.user_id == user_id,
            DataSource.provider == "wahoo",
            DataPointSeries.series_type_definition_id == get_series_type_id(series),
        )
    )
    return db.execute(q).scalar_one()


def _wahoo_ride(workout_id: int = 900, starts: str = FIT_START, url: str | None = CDN_URL) -> WahooWorkoutJSON:
    summary = make_summary(duration_total_accum="19.00", time_zone="UTC", file={"url": url} if url else None)
    return WahooWorkoutJSON.model_validate(make_workout(workout_id, starts, workout_summary=summary))


class TestWahooFitSamples:
    def test_new_workout_gets_its_stream(self, db: Session, samples_on: None, wahoo_strategy: WahooStrategy) -> None:
        user = UserFactory()
        UserConnectionFactory(user=user, provider="wahoo")
        with patch("app.services.workout_fit_samples.download_fit", return_value=make_running_fit()) as dl:
            record_id = wahoo_strategy.workouts.save_workout(db, user.id, _wahoo_ride())

        assert dl.call_args.args[0] == CDN_URL
        stream = workout_stream_service.get_stream(db, user.id, record_id)
        assert stream is not None
        assert stream["provider"] == "wahoo"
        assert stream["sample_rate_sec"] == 1.0
        assert len(stream["t"]) == 20
        assert stream["heart_rate"][:3] == [150.0, 151.0, 152.0]
        assert stream["power"][0] == 250.0
        assert stream["speed"][0] == pytest.approx(3.2)
        assert stream["cadence"][0] == 85.0
        assert stream["elevation"][0] == pytest.approx(200.0)
        assert stream["latitude"][0] == pytest.approx(50.0, abs=1e-5)
        assert stream["longitude"][0] == pytest.approx(20.0, abs=1e-5)
        assert stream["temperature"][0] == 18.0

    def test_samples_are_stored_before_workout_created_is_emitted(
        self, db: Session, samples_on: None, wahoo_strategy: WahooStrategy
    ) -> None:
        user = UserFactory()
        UserConnectionFactory(user=user, provider="wahoo")
        order: list[str] = []
        real_detail = event_record_service.create_detail
        with (
            patch(
                "app.services.providers.wahoo.workouts.ingest_workout_fit",
                side_effect=lambda *a, **k: order.append("samples") or 0,
            ),
            patch.object(
                event_record_service,
                "create_detail",
                side_effect=lambda *a, **k: order.append("detail") or real_detail(*a, **k),
            ),
        ):
            wahoo_strategy.workouts.save_workout(db, user.id, _wahoo_ride(901))
        assert order == ["samples", "detail"]

    def test_already_stored_workout_is_not_downloaded_again(
        self, db: Session, samples_on: None, wahoo_strategy: WahooStrategy
    ) -> None:
        user = UserFactory()
        UserConnectionFactory(user=user, provider="wahoo")
        with patch("app.services.workout_fit_samples.download_fit", return_value=make_running_fit()) as dl:
            wahoo_strategy.workouts.save_workout(db, user.id, _wahoo_ride(902))
            wahoo_strategy.workouts.save_workout(db, user.id, _wahoo_ride(902))  # webhook retry / re-pull
        assert dl.call_count == 1

    def test_without_file_or_flag_nothing_is_downloaded(self, db: Session, wahoo_strategy: WahooStrategy) -> None:
        user = UserFactory()
        UserConnectionFactory(user=user, provider="wahoo")
        with patch("app.services.workout_fit_samples.download_fit") as dl:
            wahoo_strategy.workouts.save_workout(db, user.id, _wahoo_ride(903))  # flag off
            with patch.object(settings, "ingest_workout_samples", True):
                wahoo_strategy.workouts.save_workout(
                    db, user.id, _wahoo_ride(904, starts="2025-06-02T08:00:00.000Z", url=None)
                )
        dl.assert_not_called()
        assert db.query(EventRecord).filter(EventRecord.external_id.in_(["903", "904"])).count() == 2

    def test_backfill_caps_fit_downloads_newest_first(
        self, db: Session, samples_on: None, wahoo_strategy: WahooStrategy
    ) -> None:
        user = UserFactory()
        UserConnectionFactory(user=user, provider="wahoo")
        listing = {
            "workouts": [
                make_workout(
                    1, "2026-07-05T08:00:00.000Z", workout_summary=make_summary(file={"url": CDN_URL + "?old"})
                ),
                make_workout(
                    2, "2026-07-10T08:00:00.000Z", workout_summary=make_summary(file={"url": CDN_URL + "?new"})
                ),
            ]
        }
        with (
            patch.object(settings, "workout_fit_downloads_per_sync", 1),
            patch.object(wahoo_strategy.workouts, "_make_api_request", return_value=listing),
            patch("app.services.workout_fit_samples.download_fit", return_value=make_running_fit()) as dl,
        ):
            count = wahoo_strategy.workouts.load_data(
                db, user.id, start_date="2026-07-01T00:00:00Z", end_date="2026-07-15T00:00:00Z"
            )
        assert count == 2  # both workouts stored
        assert [c.args[0] for c in dl.call_args_list] == [CDN_URL + "?new"]

    def test_webhook_workout_gets_its_samples(
        self, db: Session, samples_on: None, wahoo_strategy: WahooStrategy
    ) -> None:
        user = UserFactory()
        UserConnectionFactory(user=user, provider="wahoo", provider_user_id="60462")
        ride = _wahoo_ride(905).model_dump(mode="json")
        summary = {**ride.pop("workout_summary"), "workout": ride}
        payload = {"event_type": "workout_summary", "user": {"id": 60462}, "workout_summary": summary}
        with patch("app.services.workout_fit_samples.download_fit", return_value=make_running_fit()):
            out = wahoo_strategy.webhooks.process_payload(db, payload, "trace")
        assert out["status"] == "processed"
        assert _count(db, user.id, SeriesType.heart_rate) == 20

    def test_coverage_declares_the_fit_series(self) -> None:
        assert WAHOO_TIMESERIES == FIT_RECORD_SERIES
        assert {SeriesType.heart_rate, SeriesType.power, SeriesType.latitude, SeriesType.air_temperature} <= set(
            WAHOO_TIMESERIES
        )
