"""Wahoo Cloud API OAuth 2.0.

Source: https://cloud-api.wahooligan.com/#authentication

* Access tokens live 2 hours; refresh tokens are single-use and rotate on every refresh
  (the base template stores the new pair; refreshes are serialised by a distributed lock).
* Client credentials are accepted in the form body.
"""

import logging

import httpx

from app.config import settings
from app.schemas.auth import AuthenticationMethod
from app.schemas.enums import ProviderName
from app.schemas.model_crud.credentials import (
    OAuthTokenResponse,
    ProviderCredentials,
    ProviderEndpoints,
)
from app.services.providers.templates.base_oauth import BaseOAuthTemplate
from app.utils.structured_logging import log_structured

logger = logging.getLogger(__name__)


class WahooOAuth(BaseOAuthTemplate):
    """Wahoo OAuth 2.0 implementation."""

    @property
    def endpoints(self) -> ProviderEndpoints:
        return ProviderEndpoints(
            authorize_url=f"{self.api_base_url}/oauth/authorize",
            token_url=f"{self.api_base_url}/oauth/token",
        )

    @property
    def credentials(self) -> ProviderCredentials:
        return ProviderCredentials(
            client_id=settings.wahoo_client_id or "",
            client_secret=(settings.wahoo_client_secret.get_secret_value() if settings.wahoo_client_secret else ""),
            redirect_uri=settings.oauth_redirect_uri(ProviderName.WAHOO),
            default_scope=settings.wahoo_default_scope,
        )

    use_pkce: bool = False  # Supported by Wahoo only for non-confidential apps; OW runs server-side.
    auth_method: AuthenticationMethod = AuthenticationMethod.BODY

    def _get_provider_user_info(self, token_response: OAuthTokenResponse, user_id: str) -> dict[str, str | None]:
        """Fetch the Wahoo user id (``GET /v1/user``); it is the key used by webhooks."""
        try:
            response = httpx.get(
                f"{self.api_base_url}/v1/user",
                headers={"Authorization": f"Bearer {token_response.access_token}"},
                timeout=30.0,
            )
            response.raise_for_status()
            data = response.json()
            provider_user_id = data.get("id")
            return {
                "user_id": str(provider_user_id) if provider_user_id is not None else None,
                "username": None,
            }
        except Exception as e:
            log_structured(
                logger,
                "error",
                f"Failed to fetch Wahoo user: {e}",
                provider="wahoo",
                task="get_provider_user_info",
                user_id=user_id,
            )
            return {"user_id": None, "username": None}

    def deregister_user(self, access_token: str, provider_user_id: str | None = None) -> None:
        """Revoke the app's permissions for this user (``DELETE /v1/permissions``)."""
        response = httpx.delete(
            f"{self.api_base_url}/v1/permissions",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30.0,
        )
        response.raise_for_status()
