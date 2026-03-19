"""Google OAuth flow: URL generation and callback handling."""

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt
from google_auth_oauthlib.flow import Flow
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.tokens import encrypt_token
from src.config import settings
from src.state import families

logger = logging.getLogger(__name__)

# Gmail read-only + Calendar events. Radar never sends from caregiver Gmail.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.events",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]

# JWT algorithm for state parameter
_JWT_ALGORITHM = "HS256"
_STATE_EXPIRY_MINUTES = 30


def _get_signing_key() -> str:
    key = settings.token_encryption_key.get_secret_value()
    if not key:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY is not set")
    return key


def _build_flow(state: str | None = None) -> Flow:
    """Build a Google OAuth flow instance."""
    client_config = {
        "web": {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret.get_secret_value(),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [settings.google_redirect_uri],
        }
    }
    flow = Flow.from_client_config(
        client_config,
        scopes=SCOPES,
        redirect_uri=settings.google_redirect_uri,
    )
    if state:
        flow.code_verifier = None  # Not using PKCE for server-side flow
    return flow


def build_oauth_url(family_id: UUID, caregiver_phone: str) -> str:
    """Generate a Google OAuth consent URL.

    The state parameter is a JWT signed with token_encryption_key containing
    {family_id, caregiver_phone, exp}.
    """
    signing_key = _get_signing_key()
    state_payload = {
        "family_id": str(family_id),
        "caregiver_phone": caregiver_phone,
        "exp": datetime.now(UTC) + timedelta(minutes=_STATE_EXPIRY_MINUTES),
    }
    state_token = jwt.encode(state_payload, signing_key, algorithm=_JWT_ALGORITHM)

    flow = _build_flow(state=state_token)
    authorization_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=state_token,
    )
    return authorization_url


def decode_state(state_token: str) -> dict:
    """Decode and verify the state JWT. Returns {family_id, caregiver_phone}."""
    signing_key = _get_signing_key()
    payload = jwt.decode(state_token, signing_key, algorithms=[_JWT_ALGORITHM])
    return {
        "family_id": UUID(payload["family_id"]),
        "caregiver_phone": payload["caregiver_phone"],
    }


async def handle_callback(
    session: AsyncSession, code: str, state: str
) -> "families.Caregiver":
    """Exchange auth code for tokens, encrypt refresh token, store in DB.

    Returns the updated Caregiver record.
    """
    # Decode state to get family + caregiver info
    state_data = decode_state(state)
    family_id: UUID = state_data["family_id"]
    caregiver_phone: str = state_data["caregiver_phone"]

    # Look up or create caregiver
    caregiver = await families.get_caregiver_by_phone(session, family_id, caregiver_phone)
    if caregiver is None:
        caregiver = await families.create_caregiver(
            session, family_id, caregiver_phone
        )

    # Exchange code for tokens
    flow = _build_flow()
    flow.fetch_token(code=code)
    credentials = flow.credentials

    if not credentials.refresh_token:
        raise ValueError(
            "No refresh token received. User may need to revoke access and re-authorize."
        )

    # Get user email from id_token or userinfo
    google_email = _extract_email(credentials)

    # Encrypt refresh token before storage
    encrypted_refresh = encrypt_token(credentials.refresh_token)
    token_expiry = credentials.expiry or (
        datetime.now(UTC) + timedelta(hours=1)
    )
    if token_expiry.tzinfo is None:
        token_expiry = token_expiry.replace(tzinfo=UTC)

    # Store in DB
    caregiver = await families.update_caregiver_google_tokens(
        session,
        caregiver_id=caregiver.id,
        email=google_email,
        refresh_token_encrypted=encrypted_refresh,
        token_expires_at=token_expiry,
    )

    await session.commit()
    logger.info(
        "OAuth tokens stored for caregiver %s (family %s, email %s)",
        caregiver.id,
        family_id,
        google_email,
    )
    return caregiver


def _extract_email(credentials) -> str:
    """Extract the user's email from OAuth credentials."""
    # Try id_token first (from openid scope)
    if hasattr(credentials, "id_token") and credentials.id_token:
        # id_token is a dict when decoded
        if isinstance(credentials.id_token, dict):
            email = credentials.id_token.get("email")
            if email:
                return email

    # Fallback: call userinfo endpoint
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token as google_id_token

    try:
        id_info = google_id_token.verify_oauth2_token(
            credentials.token,
            google_requests.Request(),
            settings.google_client_id,
        )
        return id_info["email"]
    except Exception:
        pass

    # Last resort: use the token to call userinfo
    import httpx

    resp = httpx.get(
        "https://www.googleapis.com/oauth2/v2/userinfo",
        headers={"Authorization": f"Bearer {credentials.token}"},
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json()["email"]
