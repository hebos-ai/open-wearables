"""Workout samples from the FIT file: provider-agnostic path (Wahoo, and COROS once mapped).

The FIT files are synthetic (``tests/fixtures/fit_builder.py``): no real user data.
"""

from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch
from uuid import UUID

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import DataPointSeries, DataSource
from app.schemas.enums import SeriesType
from app.schemas.enums.series_types import get_series_type_id
from app.services.workout_fit_samples import FitTooLargeError, download_fit, ingest_workout_fit
from tests.factories import UserFactory
from tests.fixtures.fit_builder import make_running_fit

CDN_URL = "https://cdn.wahooligan.com/wahoo-cloud/workout_file/synthetic.fit"


@pytest.fixture
def samples_on() -> Iterator[None]:
    with patch.object(settings, "ingest_workout_samples", True):
        yield


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _count(db: Session, user_id: UUID, provider: str, series: SeriesType | None = None) -> int:
    q = (
        select(func.count())
        .select_from(DataPointSeries)
        .join(DataSource, DataSource.id == DataPointSeries.data_source_id)
        .where(DataSource.user_id == user_id, DataSource.provider == provider)
    )
    if series is not None:
        q = q.where(DataPointSeries.series_type_definition_id == get_series_type_id(series))
    return db.execute(q).scalar_one()


class TestDownloadFit:
    def test_returns_the_bytes(self) -> None:
        body = make_running_fit()
        assert download_fit(CDN_URL, client=_client(lambda r: httpx.Response(200, content=body))) == body

    def test_retries_429_honouring_retry_after(self) -> None:
        calls: list[int] = []
        waits: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            if len(calls) == 1:
                return httpx.Response(429, headers={"Retry-After": "7"})
            return httpx.Response(200, content=b"fit")

        assert download_fit(CDN_URL, client=_client(handler), sleep=waits.append) == b"fit"
        assert waits == [7.0]

    def test_gives_up_after_the_429_retries(self) -> None:
        waits: list[float] = []
        with pytest.raises(httpx.HTTPError):
            download_fit(CDN_URL, client=_client(lambda r: httpx.Response(429)), sleep=waits.append)
        assert len(waits) == 3
        assert all(w <= 60 for w in waits)

    def test_rejects_files_over_the_size_cap(self) -> None:
        with pytest.raises(FitTooLargeError):
            download_fit(CDN_URL, max_bytes=10, client=_client(lambda r: httpx.Response(200, content=b"x" * 11)))

    def test_http_errors_raise(self) -> None:
        with pytest.raises(httpx.HTTPStatusError):
            download_fit(CDN_URL, client=_client(lambda r: httpx.Response(403)))


class TestIngestWorkoutFit:
    """Provider-agnostic path, exercised as COROS (the handler COROS will call)."""

    def test_flag_off_downloads_nothing(self, db: Session) -> None:
        user = UserFactory()
        with patch("app.services.workout_fit_samples.download_fit") as dl:
            n = ingest_workout_fit(db, provider="coros", user_id=user.id, fit_url=CDN_URL, activity_id="1")
        assert n == 0
        dl.assert_not_called()

    def test_stores_every_record_series(self, db: Session, samples_on: None) -> None:
        user = UserFactory()
        with patch("app.services.workout_fit_samples.download_fit", return_value=make_running_fit()) as dl:
            n = ingest_workout_fit(
                db, provider="coros", user_id=user.id, fit_url=CDN_URL, activity_id="1", zone_offset="+02:00"
            )

        assert n > 0
        assert dl.call_args.args[1] == {}  # CDN link: no credentials sent
        for series in (
            SeriesType.heart_rate,
            SeriesType.power,
            SeriesType.speed,
            SeriesType.cadence,
            SeriesType.elevation,
            SeriesType.latitude,
            SeriesType.longitude,
            SeriesType.air_temperature,
        ):
            assert _count(db, user.id, "coros", series) == 20, series
        assert _count(db, user.id, "coros") == n

    def test_token_only_for_the_provider_api_host(self, db: Session, samples_on: None) -> None:
        user = UserFactory()
        with (
            patch("app.services.workout_fit_samples._get_valid_token", return_value="tok") as tok,
            patch("app.services.workout_fit_samples.download_fit", return_value=make_running_fit()) as dl,
        ):
            ingest_workout_fit(
                db,
                provider="coros",
                user_id=user.id,
                fit_url="https://api.example-coros.test/v2/fit/1",
                activity_id="1",
                api_base_url="https://api.example-coros.test",
                oauth=MagicMock(),
                connection_repo=MagicMock(),
            )
        tok.assert_called_once()
        assert dl.call_args.args[1] == {"Authorization": "Bearer tok"}

    def test_reingesting_the_same_file_does_not_duplicate(self, db: Session, samples_on: None) -> None:
        user = UserFactory()
        with patch("app.services.workout_fit_samples.download_fit", return_value=make_running_fit()):
            first = ingest_workout_fit(db, provider="coros", user_id=user.id, fit_url=CDN_URL, activity_id="1")
            ingest_workout_fit(db, provider="coros", user_id=user.id, fit_url=CDN_URL, activity_id="1")
        assert _count(db, user.id, "coros") == first

    @pytest.mark.parametrize(
        "patch_kwargs",
        [{"side_effect": httpx.ConnectError("down")}, {"return_value": b"not a fit file"}],
        ids=["download-error", "corrupt-file"],
    )
    def test_failures_are_isolated(self, db: Session, samples_on: None, patch_kwargs: dict) -> None:
        user = UserFactory()
        with patch("app.services.workout_fit_samples.download_fit", **patch_kwargs):
            n = ingest_workout_fit(db, provider="coros", user_id=user.id, fit_url=CDN_URL, activity_id="1")
        assert n == 0
        assert _count(db, user.id, "coros") == 0


def test_decimal_values_survive_the_round_trip(db: Session, samples_on: None) -> None:
    user = UserFactory()
    with patch("app.services.workout_fit_samples.download_fit", return_value=make_running_fit()):
        ingest_workout_fit(db, provider="coros", user_id=user.id, fit_url=CDN_URL, activity_id="1")
    first = db.execute(
        select(DataPointSeries.recorded_at, DataPointSeries.value)
        .join(DataSource, DataSource.id == DataPointSeries.data_source_id)
        .where(
            DataSource.user_id == user.id,
            DataPointSeries.series_type_definition_id == get_series_type_id(SeriesType.heart_rate),
        )
        .order_by(DataPointSeries.recorded_at)
        .limit(1)
    ).one()
    assert first.recorded_at == datetime(2025, 6, 1, 8, 0, tzinfo=timezone.utc)
    assert Decimal(first.value) == Decimal(150)
