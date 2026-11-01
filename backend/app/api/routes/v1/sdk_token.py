import hashlib
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Header, HTTPException, status

from app.config import settings
from app.database import DbSession
from app.schemas.auth import SDKTokenRequest, TokenResponse
from app.services import application_service, create_sdk_user_token, refresh_token_service
from app.services.api_key_service import api_key_service
from app.utils.auth import DeveloperOptionalDep

router = APIRouter()


@router.post("/users/{user_id}/token")
def create_user_token(
    user_id: UUID,
    db: DbSession,
    payload: Annotated[SDKTokenRequest | None, Body()] = None,
    developer: DeveloperOptionalDep = None,
    x_open_wearables_api_key: Annotated[str | None, Header(alias="X-Open-Wearables-API-Key")] = None,
) -> TokenResponse:
    """Exchange app credentials or admin auth for user-scoped access token.

    Supports two authentication methods:
    1. App credentials: Provide app_id and app_secret in the request body
    2. Admin authentication: Authenticate as a developer/admin via Bearer token
       (app_id and app_secret can be omitted)
    3. API key (``X-Open-Wearables-API-Key``): a backend that already holds an API key
       (it can read every user's data) mints tokens for its own users without also
       holding app credentials. The key itself never goes into the token: the token's
       ``app_id`` is ``apikey:`` + a short hash of it, for the audit trail.

    Both methods return access_token with refresh_token.

    Returns a JWT token scoped to SDK endpoints only.
    Tokens expire after configured time (default: 60 minutes).

    Args:
        user_id: OpenWearables User ID (UUID)
        payload: Optional application credentials (app_id, app_secret)
        db: Database session
        developer: Optional authenticated developer (from Bearer token)

    Returns:
        TokenResponse containing access_token, token_type, and refresh_token

    Raises:
        401: If app credentials are invalid or admin auth is missing
        400: If neither app credentials nor admin auth is provided
    """
    app_id: str

    # Method 1: App credentials provided
    if payload and payload.app_id and payload.app_secret:
        application = application_service.validate_credentials(db, payload.app_id, payload.app_secret)
        app_id = application.app_id
    # Method 2: Admin authentication (developer token)
    elif developer:
        # Use developer ID as app_id for admin-generated tokens (enables audit trail)
        # Format: "admin:{developer_id}" to distinguish from app-generated tokens
        app_id = f"admin:{developer.id}"
    # Method 3: API key of a backend service
    elif x_open_wearables_api_key:
        api_key = api_key_service.validate_api_key(db, x_open_wearables_api_key)
        app_id = "apikey:" + hashlib.sha256(api_key.id.encode()).hexdigest()[:16]
    else:
        # Neither method provided
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Either app credentials (app_id, app_secret) or admin authentication (Bearer token) is required",
        )

    # Generate user-scoped SDK token
    access_token = create_sdk_user_token(
        app_id=app_id,
        user_id=str(user_id),
    )

    refresh_token = refresh_token_service.create_sdk_refresh_token(db, user_id, app_id)

    return TokenResponse(
        access_token=access_token,
        token_type="bearer",
        refresh_token=refresh_token,
        expires_in=settings.access_token_expire_minutes * 60,
    )
