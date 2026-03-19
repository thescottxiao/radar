import hashlib
import hmac
import logging

import httpx

from src.config import settings

logger = logging.getLogger(__name__)

BASE_URL = "https://graph.facebook.com/v21.0"


async def send_message(to_phone: str, body: str) -> dict:
    """Send a free-form text message (within 24-hour window)."""
    # Meta API expects digits only, no + prefix
    clean_phone = to_phone.lstrip("+")
    url = f"{BASE_URL}/{settings.whatsapp_phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": clean_phone,
        "type": "text",
        "text": {"body": body},
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            url,
            json=payload,
            headers=_auth_headers(),
            timeout=10.0,
        )
        if resp.status_code >= 400:
            logger.error("Meta API error: %s %s", resp.status_code, resp.text)
        resp.raise_for_status()
        return resp.json()


async def send_template(
    to_phone: str, template_name: str, language: str = "en_US", components: list | None = None
) -> dict:
    """Send a template message (for bot-initiated messages outside 24h window)."""
    url = f"{BASE_URL}/{settings.whatsapp_phone_number_id}/messages"
    template = {
        "name": template_name,
        "language": {"code": language},
    }
    if components:
        template["components"] = components

    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "template",
        "template": template,
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            url,
            json=payload,
            headers=_auth_headers(),
            timeout=10.0,
        )
        resp.raise_for_status()
        return resp.json()


async def send_interactive_buttons(
    to_phone: str,
    body: str,
    buttons: list[dict[str, str]],
    header: str | None = None,
    footer: str | None = None,
) -> dict:
    """Send an interactive button message (within 24-hour window).

    buttons: list of {"id": "...", "title": "..."} dicts (max 3, title max 20 chars).
    """
    if len(buttons) > 3:
        raise ValueError("WhatsApp allows max 3 buttons per message")

    clean_phone = to_phone.lstrip("+")
    url = f"{BASE_URL}/{settings.whatsapp_phone_number_id}/messages"

    interactive: dict = {
        "type": "button",
        "body": {"text": body[:1024]},
        "action": {
            "buttons": [
                {"type": "reply", "reply": {"id": btn["id"], "title": btn["title"][:20]}}
                for btn in buttons
            ]
        },
    }
    if header:
        interactive["header"] = {"type": "text", "text": header}
    if footer:
        interactive["footer"] = {"text": footer}

    payload = {
        "messaging_product": "whatsapp",
        "to": clean_phone,
        "type": "interactive",
        "interactive": interactive,
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            url,
            json=payload,
            headers=_auth_headers(),
            timeout=10.0,
        )
        if resp.status_code >= 400:
            logger.error("Meta API error (buttons): %s %s", resp.status_code, resp.text)
        resp.raise_for_status()
        return resp.json()


def verify_webhook_signature(payload_body: bytes, signature: str) -> bool:
    """Verify Meta Cloud API webhook signature (X-Hub-Signature-256)."""
    secret = settings.whatsapp_webhook_secret.get_secret_value()
    if not secret:
        logger.warning("WHATSAPP_WEBHOOK_SECRET not set, skipping verification")
        return True
    expected = "sha256=" + hmac.new(
        secret.encode(), payload_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def _auth_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.whatsapp_api_token.get_secret_value()}",
        "Content-Type": "application/json",
    }
