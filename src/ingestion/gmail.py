"""Gmail push notification handler.

Receives Pub/Sub notifications when new emails arrive, fetches email content
via the Gmail API, and passes to the extraction pipeline.

Watch channels expire every 7 days — auto-renewed on 5-day intervals.
"""

import base64
import json
import logging
from datetime import UTC, datetime
from email.utils import parseaddr

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from src.actions.state import persist_extraction
from src.actions.whatsapp import send_buttons_to_family
from src.auth.tokens import decrypt_token
from src.config import settings
from src.extraction.email import process_email
from src.ingestion.ics import is_ics_file
from src.ingestion.schemas import EmailAttachment, EmailContent
from src.state import families as families_dal
from src.state.models import PendingActionType
from src.state.pending import create_pending_action
from src.utils.button_ids import encode_button_id

logger = logging.getLogger(__name__)

GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1"


async def handle_gmail_notification(
    session: AsyncSession, payload: dict
) -> None:
    """Handle a Gmail Pub/Sub push notification.

    1. Decode the base64 Pub/Sub message data.
    2. Look up the caregiver by email address.
    3. Fetch new messages since last historyId.
    4. Pass each message to the extraction pipeline.
    """
    # Decode Pub/Sub message
    message_data = payload.get("message", {}).get("data", "")
    if not message_data:
        logger.warning("Empty Pub/Sub message data")
        return

    try:
        decoded = json.loads(base64.b64decode(message_data))
    except (json.JSONDecodeError, Exception) as e:
        logger.error("Failed to decode Pub/Sub message: %s", e)
        return

    email_address = decoded.get("emailAddress", "")
    history_id = decoded.get("historyId")

    if not email_address:
        logger.warning("No emailAddress in Pub/Sub notification")
        return

    # Look up caregiver
    caregiver = await families_dal.get_caregiver_by_email(session, email_address)
    if caregiver is None:
        logger.warning("No caregiver found for email: %s", email_address)
        return

    if caregiver.google_refresh_token_encrypted is None:
        logger.warning("No Google tokens for caregiver %s", caregiver.id)
        return

    # Get access token
    access_token = await _get_access_token(caregiver)

    # Fetch new messages since last historyId
    last_history_id = caregiver.gmail_watch_history_id
    if last_history_id is None:
        logger.info(
            "No previous historyId for caregiver %s, fetching latest messages",
            caregiver.id,
        )
        last_history_id = int(history_id) - 1 if history_id else None

    if last_history_id is None:
        return

    message_ids = await _get_new_message_ids(
        access_token, last_history_id
    )

    # Update historyId
    if history_id:
        caregiver.gmail_watch_history_id = int(history_id)
        await session.flush()

    # Process each new message
    for msg_id in message_ids:
        try:
            email_content = await fetch_email_content(access_token, msg_id)
            if email_content is None:
                continue

            email = EmailContent(**email_content)

            # Process ICS attachments before normal email extraction
            if email.attachments:
                await _process_ics_attachments(
                    session,
                    caregiver.family_id,
                    access_token,
                    msg_id,
                    email.attachments,
                )

            result = await process_email(
                session, caregiver.family_id, email, source="email"
            )

            if result.is_relevant:
                # Persist action items and learnings (events go through button confirmation)
                await persist_extraction(
                    session,
                    caregiver.family_id,
                    result,
                    source="email",
                    source_ref=msg_id,
                    skip_events=True,
                )

                # Send button confirmation for each extracted event
                for ev in result.events:
                    if ev.datetime_start is None:
                        logger.warning("Skipping event '%s' — no datetime_start", ev.title)
                        continue

                    pending = await create_pending_action(
                        session,
                        family_id=caregiver.family_id,
                        action_type=PendingActionType.event_confirmation,
                        draft_content=f"{ev.title} — {ev.datetime_start.strftime('%b %d, %I:%M %p')}",
                        context={
                            "event_data": ev.model_dump(mode="json"),
                            "email_subject": email.subject,
                            "source_ref": msg_id,
                        },
                    )

                    time_str = ev.datetime_start.strftime("%b %d, %I:%M %p")
                    body = f"New event from email:\n*{ev.title}*\n{time_str}"
                    if ev.location:
                        body += f"\n📍 {ev.location}"
                    body += "\n\nAdd to your calendar?"

                    buttons = [
                        {"id": encode_button_id("event_confirm", str(pending.id), "yes"), "title": "Yes, add it"},
                        {"id": encode_button_id("event_confirm", str(pending.id), "no"), "title": "No, skip"},
                    ]

                    try:
                        await send_buttons_to_family(session, caregiver.family_id, body, buttons)
                    except Exception:
                        logger.exception("Failed to send button message for event '%s'", ev.title)

                logger.info(
                    "Processed Gmail message %s for family %s: %d events, %d items",
                    msg_id,
                    caregiver.family_id,
                    len(result.events),
                    len(result.action_items),
                )
        except Exception:
            logger.exception("Error processing Gmail message %s", msg_id)


async def setup_gmail_watch(session: AsyncSession, caregiver) -> None:
    """Create a Gmail Pub/Sub watch for a caregiver's inbox.

    The watch monitors for new messages and sends notifications to our Pub/Sub topic.
    Watches expire after 7 days.
    """
    if caregiver.google_refresh_token_encrypted is None:
        logger.warning("Cannot setup watch: no tokens for caregiver %s", caregiver.id)
        return

    access_token = await _get_access_token(caregiver)

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{GMAIL_API_BASE}/users/me/watch",
            headers={"Authorization": f"Bearer {access_token}"},
            json={
                "topicName": settings.gmail_pubsub_topic,
                "labelIds": ["INBOX"],
            },
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()

    # Update caregiver with watch info
    caregiver.gmail_watch_history_id = int(data.get("historyId", 0))
    caregiver.gmail_watch_expiry = datetime.fromtimestamp(
        int(data.get("expiration", 0)) / 1000, tz=UTC
    )
    await session.flush()

    logger.info(
        "Gmail watch created for caregiver %s, expires %s",
        caregiver.id,
        caregiver.gmail_watch_expiry,
    )


async def renew_gmail_watch(session: AsyncSession, caregiver) -> None:
    """Renew a Gmail watch that is expiring.

    Gmail watches expire every 7 days. We renew on 5-day intervals.
    Renewal is done by simply creating a new watch (Gmail replaces the old one).
    """
    await setup_gmail_watch(session, caregiver)
    logger.info("Renewed Gmail watch for caregiver %s", caregiver.id)


async def fetch_email_content(
    access_token: str, message_id: str
) -> dict | None:
    """Fetch and parse a single email from Gmail API.

    Returns a dict matching EmailContent fields, or None if parsing fails.
    Never stores raw email content — only extracts structured fields.
    """
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{GMAIL_API_BASE}/users/me/messages/{message_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"format": "full"},
            timeout=10.0,
        )
        if resp.status_code == 404:
            logger.warning("Gmail message %s not found", message_id)
            return None
        resp.raise_for_status()
        data = resp.json()

    # Parse headers
    headers = {
        h["name"].lower(): h["value"]
        for h in data.get("payload", {}).get("headers", [])
    }

    from_address = headers.get("from", "")
    # Extract just the email address from "Name <email>" format
    _, from_email = parseaddr(from_address)

    to_raw = headers.get("to", "")
    to_addresses = [parseaddr(addr.strip())[1] for addr in to_raw.split(",") if addr.strip()]

    subject = headers.get("subject", "")
    date_str = headers.get("date")
    date = None
    if date_str:
        from email.utils import parsedate_to_datetime

        try:
            date = parsedate_to_datetime(date_str)
        except (ValueError, TypeError):
            pass

    # Extract body
    body_text, body_html = _extract_body(data.get("payload", {}))

    # Extract attachment metadata
    attachments = _extract_attachments(data.get("payload", {}))

    return {
        "message_id": message_id,
        "from_address": from_email or from_address,
        "to_addresses": to_addresses,
        "subject": subject,
        "body_text": body_text,
        "body_html": body_html,
        "date": date,
        "attachments": attachments,
    }


def _extract_body(payload: dict) -> tuple[str, str]:
    """Extract text and HTML body from Gmail message payload."""
    body_text = ""
    body_html = ""

    mime_type = payload.get("mimeType", "")

    # Single part message
    if "body" in payload and payload["body"].get("data"):
        decoded = base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
        if "html" in mime_type:
            body_html = decoded
        else:
            body_text = decoded
        return body_text, body_html

    # Multipart message
    for part in payload.get("parts", []):
        part_mime = part.get("mimeType", "")
        part_data = part.get("body", {}).get("data", "")

        if part_data:
            decoded = base64.urlsafe_b64decode(part_data).decode("utf-8", errors="replace")
            if part_mime == "text/plain" and not body_text:
                body_text = decoded
            elif part_mime == "text/html" and not body_html:
                body_html = decoded

        # Nested multipart
        if "parts" in part:
            nested_text, nested_html = _extract_body(part)
            if not body_text and nested_text:
                body_text = nested_text
            if not body_html and nested_html:
                body_html = nested_html

    return body_text, body_html


def _extract_attachments(payload: dict) -> list[dict]:
    """Extract attachment metadata from Gmail message payload.

    Recursively walks MIME parts looking for parts with filenames and
    attachmentIds. Returns metadata only — content is downloaded on demand.
    """
    attachments: list[dict] = []
    _walk_for_attachments(payload, attachments)
    return attachments


def _walk_for_attachments(part: dict, attachments: list[dict]) -> None:
    """Recursively walk MIME parts to find attachments."""
    filename = part.get("filename", "")
    body = part.get("body", {})
    attachment_id = body.get("attachmentId", "")

    if filename and attachment_id:
        attachments.append({
            "filename": filename,
            "mime_type": part.get("mimeType", ""),
            "attachment_id": attachment_id,
            "size": body.get("size", 0),
        })

    for sub_part in part.get("parts", []):
        _walk_for_attachments(sub_part, attachments)


async def download_gmail_attachment(
    access_token: str, message_id: str, attachment_id: str
) -> str:
    """Download a single Gmail attachment by ID. Returns content as string."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{GMAIL_API_BASE}/users/me/messages/{message_id}/attachments/{attachment_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()

    raw = base64.urlsafe_b64decode(data.get("data", ""))
    return raw.decode("utf-8", errors="replace")


async def _process_ics_attachments(
    session: AsyncSession,
    family_id,
    access_token: str,
    message_id: str,
    attachments: list[EmailAttachment],
) -> None:
    """Download and process ICS attachments from a Gmail message."""
    from src.ingestion.ics import MAX_ICS_SIZE, process_ics_attachment, send_ics_batch_confirmation

    ics_attachments = [a for a in attachments if is_ics_file(a.filename, a.mime_type)]
    if not ics_attachments:
        return

    for att in ics_attachments:
        try:
            # Skip attachments that are clearly too large before downloading
            if att.size > MAX_ICS_SIZE:
                logger.info("Skipping oversized ICS attachment '%s' (%d bytes)", att.filename, att.size)
                continue

            ics_content = await download_gmail_attachment(
                access_token, message_id, att.attachment_id
            )
            results = await process_ics_attachment(session, family_id, ics_content)

            if not results:
                logger.info(
                    "No events found in ICS attachment '%s' from message %s",
                    att.filename, message_id,
                )
                continue

            new_events = [event for event, is_new in results if is_new]
            if not new_events:
                continue

            try:
                await send_ics_batch_confirmation(
                    session, family_id, new_events, att.filename,
                    source_label=f"an email attachment ({att.filename})",
                    extra_context={"email_message_id": message_id},
                )
            except Exception:
                logger.exception(
                    "Failed to send ICS batch confirmation for '%s'", att.filename
                )

            logger.info(
                "Processed ICS attachment '%s' from message %s: %d events, %d new",
                att.filename, message_id, len(results), len(new_events),
            )
        except Exception:
            logger.exception(
                "Failed to process ICS attachment '%s' from message %s",
                att.filename, message_id,
            )


async def _get_access_token(caregiver) -> str:
    """Get a valid access token for a caregiver, refreshing if needed."""
    refresh_token = decrypt_token(caregiver.google_refresh_token_encrypted)

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret.get_secret_value(),
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()

    return data["access_token"]


async def _get_new_message_ids(
    access_token: str, history_id: int
) -> list[str]:
    """Get message IDs added since the given historyId."""
    message_ids = []

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{GMAIL_API_BASE}/users/me/history",
            headers={"Authorization": f"Bearer {access_token}"},
            params={
                "startHistoryId": str(history_id),
                "historyTypes": "messageAdded",
                "labelId": "INBOX",
            },
            timeout=10.0,
        )
        if resp.status_code == 404:
            # historyId too old, need to re-sync
            logger.warning("Gmail historyId %s too old, skipping", history_id)
            return []
        resp.raise_for_status()
        data = resp.json()

    for history_record in data.get("history", []):
        for msg_added in history_record.get("messagesAdded", []):
            msg = msg_added.get("message", {})
            msg_id = msg.get("id")
            if msg_id:
                # Skip messages in SPAM or TRASH
                labels = msg.get("labelIds", [])
                if "SPAM" not in labels and "TRASH" not in labels:
                    message_ids.append(msg_id)

    return message_ids
