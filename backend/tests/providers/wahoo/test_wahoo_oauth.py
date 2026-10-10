"""Wahoo OAuth: endpoints, credentials-in-body and user id lookup."""

from unittest.mock import MagicMock, patch

from app.schemas.auth import AuthenticationMethod
from app.schemas.model_crud.credentials import OAuthTokenResponse
from app.services.providers.wahoo.strategy import WahooStrategy


def test_endpoints_and_auth_method(wahoo_strategy: WahooStrategy) -> None:
    oauth = wahoo_strategy.oauth
    assert oauth is not None
    assert oauth.endpoints.authorize_url == "https://api.wahooligan.com/oauth/authorize"
    assert oauth.endpoints.token_url == "https://api.wahooligan.com/oauth/token"
    assert oauth.auth_method == AuthenticationMethod.BODY
    assert oauth.use_pkce is False
    assert "offline_data" in oauth.credentials.default_scope.split()


def test_refresh_request_sends_credentials_in_body(wahoo_strategy: WahooStrategy) -> None:
    assert wahoo_strategy.oauth is not None
    data, headers = wahoo_strategy.oauth._prepare_refresh_request("r-token")
    assert data["grant_type"] == "refresh_token"
    assert data["refresh_token"] == "r-token"
    assert "client_id" in data
    assert "client_secret" in data
    assert "Authorization" not in headers


def test_user_info_uses_v1_user(wahoo_strategy: WahooStrategy) -> None:
    assert wahoo_strategy.oauth is not None
    response = MagicMock()
    response.json.return_value = {"id": 60462, "first": "Bob"}
    with patch("app.services.providers.wahoo.oauth.httpx.get", return_value=response) as get:
        info = wahoo_strategy.oauth._get_provider_user_info(
            OAuthTokenResponse(access_token="a", token_type="bearer", expires_in=7200), "u1"
        )
    assert info == {"user_id": "60462", "username": None}
    assert get.call_args.args[0] == "https://api.wahooligan.com/v1/user"


def test_user_info_failure_returns_none(wahoo_strategy: WahooStrategy) -> None:
    assert wahoo_strategy.oauth is not None
    with patch("app.services.providers.wahoo.oauth.httpx.get", side_effect=RuntimeError("down")):
        info = wahoo_strategy.oauth._get_provider_user_info(
            OAuthTokenResponse(access_token="a", token_type="bearer", expires_in=7200), "u1"
        )
    assert info == {"user_id": None, "username": None}


def test_deregister_revokes_permissions(wahoo_strategy: WahooStrategy) -> None:
    assert wahoo_strategy.oauth is not None
    with patch("app.services.providers.wahoo.oauth.httpx.delete") as delete:
        wahoo_strategy.oauth.deregister_user("a-token")
    assert delete.call_args.args[0] == "https://api.wahooligan.com/v1/permissions"
    assert delete.call_args.kwargs["headers"] == {"Authorization": "Bearer a-token"}


def test_strategy_capabilities(wahoo_strategy: WahooStrategy) -> None:
    caps = wahoo_strategy.capabilities
    assert caps.rest_pull
    assert caps.webhook_stream
    assert not caps.webhook_registration_api
    assert wahoo_strategy.data_247 is None
    assert wahoo_strategy.supports_planned_workouts
