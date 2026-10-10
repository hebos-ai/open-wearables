"""COROS scaffold: registered, configuration-driven, and explicit about pending partner access."""

from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.config import settings
from app.schemas.planned_workouts import PlannedWorkoutPushRequest
from app.services.providers.coros.planned_workouts import CorosPlannedWorkouts
from app.services.providers.coros.strategy import CorosStrategy
from app.services.providers.factory import ProviderFactory
from app.utils.exceptions import UnsupportedProviderError


@pytest.fixture
def coros() -> CorosStrategy:
    strategy = ProviderFactory().get_provider("coros")
    assert isinstance(strategy, CorosStrategy)
    return strategy


def test_identity_and_no_data_handlers(coros: CorosStrategy) -> None:
    assert coros.name == "coros"
    assert coros.display_name == "COROS"
    assert coros.has_cloud_api
    assert coros.workouts is None
    assert coros.data_247 is None
    assert coros.webhooks is None
    assert not coros.supports_planned_workouts
    assert coros.default_live_sync_mode is None


def test_historical_sync_is_unsupported(coros: CorosStrategy) -> None:
    with pytest.raises(UnsupportedProviderError):
        coros.start_historical_sync(uuid4(), 30)


def test_oauth_unconfigured_is_503(coros: CorosStrategy) -> None:
    assert coros.oauth is not None
    with patch.object(settings, "coros_authorize_url", None), pytest.raises(HTTPException) as exc:
        _ = coros.oauth.endpoints
    assert exc.value.status_code == 503
    assert "partner" in exc.value.detail


def test_oauth_endpoints_come_from_settings(coros: CorosStrategy) -> None:
    assert coros.oauth is not None
    with (
        patch.object(settings, "coros_authorize_url", "https://partner.example/oauth/authorize"),
        patch.object(settings, "coros_token_url", "https://partner.example/oauth/token"),
    ):
        endpoints = coros.oauth.endpoints
    assert endpoints.authorize_url == "https://partner.example/oauth/authorize"
    assert endpoints.token_url == "https://partner.example/oauth/token"


def test_planned_workout_scaffold_refuses_to_guess(coros: CorosStrategy) -> None:
    assert coros.oauth is not None
    scaffold = CorosPlannedWorkouts(
        connection_repo=coros.connection_repo, provider_name="coros", api_base_url="", oauth=coros.oauth
    )
    request = PlannedWorkoutPushRequest.model_validate(
        {"workout": {"name": "x", "sport": "run", "steps": [{"kind": "interval", "durationSec": 60}]}}
    )
    with pytest.raises(UnsupportedProviderError, match="partner"):
        scaffold.push(None, uuid4(), request)  # type: ignore[arg-type]
