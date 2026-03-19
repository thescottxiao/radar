"""Google API client helpers: credentials management and service builders."""

import logging
from uuid import UUID

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import Resource, build
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.tokens import decrypt_token
from src.config import settings
from src.state.models import Caregiver

logger = logging.getLogger(__name__)

# Same scopes used during OAuth
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.events",
]


async def get_google_credentials(
    session: AsyncSession, caregiver_id: UUID
) -> Credentials:
    """Decrypt stored refresh token and create a Credentials object.

    Automatically refreshes the access token if expired.
    """
    caregiver = await session.get(Caregiver, caregiver_id)
    if caregiver is None:
        raise ValueError(f"Caregiver {caregiver_id} not found")
    if caregiver.google_refresh_token_encrypted is None:
        raise ValueError(
            f"Caregiver {caregiver_id} has no Google tokens. "
            "They need to complete the OAuth flow."
        )

    refresh_token = decrypt_token(caregiver.google_refresh_token_encrypted)

    credentials = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret.get_secret_value(),
        scopes=SCOPES,
    )

    # Refresh to get a valid access token
    if not credentials.valid:
        credentials.refresh(Request())
        logger.debug("Refreshed Google access token for caregiver %s", caregiver_id)

    return credentials


def get_calendar_service(credentials: Credentials) -> Resource:
    """Build a Google Calendar API v3 service."""
    return build("calendar", "v3", credentials=credentials, cache_discovery=False)


def get_gmail_service(credentials: Credentials) -> Resource:
    """Build a Gmail API v1 service."""
    return build("gmail", "v1", credentials=credentials, cache_discovery=False)
