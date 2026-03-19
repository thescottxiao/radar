"""OAuth FastAPI routes for Google authentication."""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.oauth import build_oauth_url, handle_callback
from src.db import get_session
from src.whatsapp_client import send_message

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

_SUCCESS_HTML = """\
<!DOCTYPE html>
<html>
<head>
    <title>Radar — Connected</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
            margin: 0;
            background: #f5f5f5;
            color: #333;
        }
        .card {
            background: white;
            border-radius: 16px;
            padding: 48px;
            text-align: center;
            box-shadow: 0 2px 12px rgba(0,0,0,0.1);
            max-width: 400px;
        }
        .checkmark { font-size: 48px; margin-bottom: 16px; }
        h1 { font-size: 24px; margin-bottom: 8px; }
        p { color: #666; line-height: 1.5; }
    </style>
</head>
<body>
    <div class="card">
        <div class="checkmark">&#10003;</div>
        <h1>Google Account Connected</h1>
        <p>You can close this window and return to WhatsApp. Radar will now sync your calendar and monitor your email for kid-related events.</p>
    </div>
</body>
</html>
"""


@router.get("/google")
async def google_oauth_start(
    family_id: UUID = Query(..., description="Family UUID"),
    caregiver_phone: str = Query(..., description="Caregiver WhatsApp phone number"),
) -> RedirectResponse:
    """Redirect to Google OAuth consent screen."""
    try:
        url = build_oauth_url(family_id, caregiver_phone)
        return RedirectResponse(url=url, status_code=302)
    except Exception:
        logger.exception("Failed to build OAuth URL")
        raise HTTPException(status_code=500, detail="Failed to initiate Google sign-in")


@router.get("/google/callback")
async def google_oauth_callback(
    code: str = Query(...),
    state: str = Query(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Handle Google OAuth callback: exchange code, store tokens, confirm via WhatsApp."""
    try:
        caregiver = await handle_callback(session, code, state)

        # Send WhatsApp confirmation to the caregiver
        display_name = caregiver.name or caregiver.google_account_email or "Caregiver"
        try:
            await send_message(
                caregiver.whatsapp_phone,
                f"{display_name} connected Google account successfully. "
                f"Calendar and email sync is now active.",
            )
        except Exception:
            logger.warning(
                "Could not send WhatsApp confirmation to %s",
                caregiver.whatsapp_phone,
            )

        return HTMLResponse(content=_SUCCESS_HTML, status_code=200)

    except ValueError as exc:
        logger.warning("OAuth callback error: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        logger.exception("OAuth callback failed")
        raise HTTPException(
            status_code=500,
            detail="Something went wrong connecting your Google account. Please try again.",
        )
