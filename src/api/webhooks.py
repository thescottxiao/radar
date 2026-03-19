"""WhatsApp webhook endpoints for Meta Cloud API."""

import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import PlainTextResponse

from src.config import settings
from src.ingestion.whatsapp import handle_whatsapp_message
from src.whatsapp_client import send_message, verify_webhook_signature

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.get("/whatsapp")
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

    # Verify webhook signature (skip if secret not configured — local dev)
    signature = request.headers.get("X-Hub-Signature-256", "")
    if settings.whatsapp_webhook_secret and not verify_webhook_signature(body, signature):
        logger.warning("Invalid WhatsApp webhook signature")
        return {"status": "ok"}
    elif not settings.whatsapp_webhook_secret:
        logger.debug("WHATSAPP_WEBHOOK_SECRET not set, skipping signature verification")

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

                logger.info("=" * 60)
                logger.info("BOT RESPONSE: %s", response)
                logger.info("=" * 60)
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


@router.post("/gcal")
async def gcal_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
) -> PlainTextResponse:
    """Receive Google Calendar push notifications.

    Google sends POST requests when calendar events change. We must always
    return 200 — non-2xx responses cause Google to back off and eventually
    stop delivering notifications.
    """
    headers = dict(request.headers)
    try:
        body = await request.json()
    except Exception:
        body = {}

    channel_id = headers.get("x-goog-channel-id", "")
    resource_state = headers.get("x-goog-resource-state", "")
    logger.info(
        "GCal webhook received: channel=%s, state=%s",
        channel_id,
        resource_state,
    )

    background_tasks.add_task(_process_gcal_notification, headers, body)
    return PlainTextResponse("ok", status_code=200)


async def _process_gcal_notification(headers: dict, body: dict) -> None:
    """Process a GCal push notification in the background."""
    from src.db import async_session_factory
    from src.ingestion.gcal import handle_gcal_notification

    try:
        async with async_session_factory() as session:
            async with session.begin():
                await handle_gcal_notification(session, headers, body)
    except Exception:
        logger.exception("Failed to process GCal notification")


@router.post("/gmail")
async def gmail_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict:
    """Receive Gmail Pub/Sub push notifications.

    Google Pub/Sub sends POST with a base64-encoded message containing
    the email address and historyId. Always return 200 to acknowledge.
    """
    payload = await request.json()
    logger.info("Gmail Pub/Sub notification received")

    background_tasks.add_task(_process_gmail_notification, payload)
    return {"status": "ok"}


async def _process_gmail_notification(payload: dict) -> None:
    """Process a Gmail push notification in the background."""
    from src.db import async_session_factory
    from src.ingestion.gmail import handle_gmail_notification

    try:
        async with async_session_factory() as session:
            async with session.begin():
                await handle_gmail_notification(session, payload)
    except Exception:
        logger.exception("Failed to process Gmail notification")


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
