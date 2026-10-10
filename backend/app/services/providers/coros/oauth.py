from fastapi import HTTPException, status

from app.config import settings
from app.schemas.auth import AuthenticationMethod
from app.schemas.enums import ProviderName
from app.schemas.model_crud.credentials import (
    OAuthTokenResponse,
    ProviderCredentials,
    ProviderEndpoints,
)
from app.services.providers.coros.pending import PARTNER_ACCESS_HINT
from app.services.providers.templates.base_oauth import BaseOAuthTemplate


class CorosOAuth(BaseOAuthTemplate):
    """COROS OAuth 2.0 — endpoints come from configuration once partner access is granted.

    ASSUMPTIONS (to confirm with the partner documentation):
    * standard authorization-code grant, credentials in the form body;
    * no PKCE.
    """

    @property
    def endpoints(self) -> ProviderEndpoints:
        if not (settings.coros_authorize_url and settings.coros_token_url):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"COROS is not configured: set COROS_AUTHORIZE_URL and COROS_TOKEN_URL ({PARTNER_ACCESS_HINT}).",
            )
        return ProviderEndpoints(authorize_url=settings.coros_authorize_url, token_url=settings.coros_token_url)

    @property
    def credentials(self) -> ProviderCredentials:
        return ProviderCredentials(
            client_id=settings.coros_client_id or "",
            client_secret=(settings.coros_client_secret.get_secret_value() if settings.coros_client_secret else ""),
            redirect_uri=settings.oauth_redirect_uri(ProviderName.COROS),
            default_scope=settings.coros_default_scope,
        )

    use_pkce: bool = False
    auth_method: AuthenticationMethod = AuthenticationMethod.BODY

    def _get_provider_user_info(self, token_response: OAuthTokenResponse, user_id: str) -> dict[str, str | None]:
        # The user-id field of the COROS token/profile response is not public yet. Keep whatever
        # the token response exposes under a conventional key, never a guessed endpoint.
        extra = token_response.model_extra or {}
        for key in ("openId", "open_id", "user_id", "userId"):
            if extra.get(key) is not None:
                return {"user_id": str(extra[key]), "username": None}
        return {"user_id": None, "username": None}
