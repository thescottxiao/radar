"""WhatsApp webhook endpoints for Meta Cloud API."""

import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import PlainTextResponse

from src.config import settings
from src.ingestion.whatsapp import handle_whatsapp_message
from src.whatsapp_client import send_message, verify_webhook_signature

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.get("/whatsapp/verify")
async def whatsapp_verify(
    request: Request,
) -> PlainTextResponse:
    """Meta webhook verification endpoint.

    Meta sends a GET request with hub.mode, hub.verify_token, and hub.challenge
    during webhook setup. We must echo back the challenge if the token matches.
    """
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode == "subscribe" and token == settings.whatsapp_verify_token:
        logger.info("WhatsApp webhook verified successfully")
        return PlainTextResponse(content=challenge or "", status_code=200)

    logger.warning("WhatsApp webhook verification failed: mode=%s", mode)
    raise HTTPException(status_code=403, detail="Verification failed")


@router.post("/whatsapp")
async def whatsapp_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict:
    """Receive inbound WhatsApp messages from Meta Cloud API.

    Validates the webhook signature, then processes the message asynchronously
    via BackgroundTasks. Returns 200 immediately per Meta requirements.
    """
    body = await request.body()

    # Verify webhook signature
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not verify_webhook_signature(body, signature):
        logger.warning("Invalid WhatsApp webhook signature")
        # Still return 200 to avoid Meta retry storms, but log the issue
        return {"status": "ok"}

    payload = await request.json()

    # Queue async processing
    background_tasks.add_task(_process_whatsapp_message, payload)

    return {"status": "ok"}


async def _process_whatsapp_message(payload: dict) -> None:
    """Process a WhatsApp message in the background.

    Creates its own database session since this runs outside the request lifecycle.
    """
    from src.db import async_session_factory

    try:
        async with async_session_factory() as session:
            async with session.begin():
                response = await handle_whatsapp_message(session, payload)

                if response:
                    # Extract sender phone to reply
                    sender_phone = _extract_sender_phone(payload)
                    if sender_phone:
                        try:
                            await send_message(sender_phone, response)
                        except Exception:
                            logger.exception(
                                "Failed to send WhatsApp response to %s",
                                sender_phone,
                            )
    except Exception:
        logger.exception("Failed to process WhatsApp message")


def _extract_sender_phone(payload: dict) -> str | None:
    """Extract the sender's phone number from the webhook payload."""
    try:
        entry = payload.get("entry", [])
        if not entry:
            return None
        changes = entry[0].get("changes", [])
        if not changes:
            return None
        value = changes[0].get("value", {})
        messages = value.get("messages", [])
        if not messages:
            return None
        phone = messages[0].get("from", "")
        if phone and not phone.startswith("+"):
            phone = f"+{phone}"
        return phone or None
    except (IndexError, KeyError, TypeError):
        return None
