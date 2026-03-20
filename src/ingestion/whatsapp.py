"""WhatsApp message ingestion: receives and processes inbound messages."""

import logging
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.extraction.router import classify_intent, route_intent
from src.state import families as families_dal
from src.state import memory as memory_dal

logger = logging.getLogger(__name__)


async def handle_whatsapp_message(
    session: AsyncSession, payload: dict
) -> str | None:
    """Process an inbound WhatsApp message (Meta Cloud API format).

    Steps:
    1. Extract message from webhook payload
    2. Look up family by group_id or sender phone
    3. Look up / create caregiver by phone
    4. If family not onboarded, delegate to onboarding
    5. Store in conversation memory
    6. Pass to intent router
    7. Return response text

    Returns response text or None if the message should not be responded to.
    """
    # Extract message details from Meta Cloud API payload
    message_data = _extract_message_from_payload(payload)
    if message_data is None:
        logger.debug("No actionable message in payload")
        return None

    sender_phone = message_data["sender_phone"]
    message_text = message_data["text"]
    group_id = message_data.get("group_id")
    sender_name = message_data.get("sender_name")
    button_reply_id = message_data.get("button_reply_id")

    # Look up family
    family = None
    if group_id:
        family = await families_dal.get_family_by_group_id(session, group_id)

    # If no group_id or family not found, try to find by caregiver phone (DMs)
    if family is None:
        caregiver = await families_dal.find_caregiver_by_phone(session, sender_phone)
        if caregiver:
            family = await families_dal.get_family(session, caregiver.family_id)

    if family is None:
        logger.info(
            "No family found for group_id=%s, phone=%s. May need onboarding.",
            group_id,
            sender_phone,
        )
        return (
            "Welcome to Radar! It looks like your family hasn't been set up yet. "
            "Please contact support to get started."
        )

    # Look up or create caregiver
    caregiver = await families_dal.get_caregiver_by_phone(
        session, family.id, sender_phone
    )
    if caregiver is None:
        caregiver = await families_dal.create_caregiver(
            session,
            family_id=family.id,
            whatsapp_phone=sender_phone,
            name=sender_name,
        )
        logger.info(
            "Created new caregiver %s for family %s (phone: %s)",
            caregiver.id,
            family.id,
            sender_phone,
        )

    # Check if family is onboarded
    if not family.onboarding_complete:
        return await _handle_onboarding(session, family, caregiver, message_text)

    # Handle ICS document uploads before intent routing
    if message_data.get("document"):
        response = await _handle_ics_upload(
            session, family, caregiver, message_data["document"]
        )
        await session.commit()
        return response

    # Store message in conversation memory
    memory_content = f"{caregiver.name or sender_phone}: {message_text}"
    await memory_dal.store_message(
        session,
        family_id=family.id,
        content=memory_content,
        msg_type="short_term",
        expires_at=datetime.now(UTC) + timedelta(hours=24),
    )

    # Classify intent and route
    intent = await classify_intent(
        session,
        family_id=family.id,
        message=message_text,
        sender_id=caregiver.id,
        button_reply_id=button_reply_id,
    )

    logger.info(
        "Classified intent: %s (confidence=%.2f) for family %s",
        intent.intent,
        intent.confidence,
        family.id,
    )

    response = await route_intent(
        session,
        family_id=family.id,
        intent=intent,
        message=message_text,
        sender_id=caregiver.id,
    )

    # Store bot response in memory too
    if response:
        await memory_dal.store_message(
            session,
            family_id=family.id,
            content=f"Radar: {response}",
            msg_type="short_term",
            expires_at=datetime.now(UTC) + timedelta(hours=24),
        )

    await session.commit()
    return response


def _extract_message_from_payload(payload: dict) -> dict | None:
    """Extract message details from Meta Cloud API webhook payload.

    Returns dict with keys: sender_phone, text, group_id (optional), sender_name (optional)
    or None if no actionable message found.
    """
    # Meta Cloud API format:
    # {
    #   "object": "whatsapp_business_account",
    #   "entry": [{
    #     "id": "...",
    #     "changes": [{
    #       "value": {
    #         "messaging_product": "whatsapp",
    #         "metadata": {"display_phone_number": "...", "phone_number_id": "..."},
    #         "contacts": [{"profile": {"name": "..."}, "wa_id": "..."}],
    #         "messages": [{
    #           "from": "15551234567",
    #           "id": "...",
    #           "timestamp": "...",
    #           "type": "text",
    #           "text": {"body": "..."}
    #         }]
    #       },
    #       "field": "messages"
    #     }]
    #   }]
    # }
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

        msg = messages[0]
        msg_type = msg.get("type", "")

        # Handle text messages
        button_reply_id = None

        if msg_type == "text":
            text = msg.get("text", {}).get("body", "")
        elif msg_type == "interactive":
            # Button replies or list replies
            interactive = msg.get("interactive", {})
            if "button_reply" in interactive:
                text = interactive["button_reply"].get("title", "")
                button_reply_id = interactive["button_reply"].get("id")
            elif "list_reply" in interactive:
                text = interactive["list_reply"].get("title", "")
            else:
                return None
        elif msg_type == "document":
            document = msg.get("document", {})
            filename = document.get("filename", "")
            mime_type_doc = document.get("mime_type", "")
            media_id = document.get("id", "")

            if not _is_ics_file(filename, mime_type_doc):
                logger.debug("Unsupported document type: %s (%s)", filename, mime_type_doc)
                return None

            sender_phone = msg.get("from", "")
            contacts = value.get("contacts", [])
            sender_name = None
            if contacts:
                sender_name = contacts[0].get("profile", {}).get("name")
            metadata = value.get("metadata", {})
            group_id = metadata.get("group_id")
            if sender_phone and not sender_phone.startswith("+"):
                sender_phone = f"+{sender_phone}"

            return {
                "sender_phone": sender_phone,
                "text": "",
                "group_id": group_id,
                "sender_name": sender_name,
                "button_reply_id": None,
                "document": {
                    "media_id": media_id,
                    "filename": filename,
                    "mime_type": mime_type_doc,
                },
            }
        else:
            # Audio, image, etc. — not handled yet
            # Voice notes will be handled in Phase 4
            logger.debug("Unsupported message type: %s", msg_type)
            return None

        if not text:
            return None

        sender_phone = msg.get("from", "")
        contacts = value.get("contacts", [])
        sender_name = None
        if contacts:
            sender_name = contacts[0].get("profile", {}).get("name")

        # Extract group_id from metadata if this is a group message
        metadata = value.get("metadata", {})
        group_id = metadata.get("group_id")

        # Normalize phone to include +
        if sender_phone and not sender_phone.startswith("+"):
            sender_phone = f"+{sender_phone}"

        return {
            "sender_phone": sender_phone,
            "text": text.strip(),
            "group_id": group_id,
            "sender_name": sender_name,
            "button_reply_id": button_reply_id,
        }

    except (IndexError, KeyError, TypeError):
        logger.exception("Failed to extract message from payload")
        return None


async def _handle_onboarding(
    session: AsyncSession, family, caregiver, message: str
) -> str:
    """Handle messages from families that haven't completed onboarding.

    Simple Phase 1 implementation: prompt for kids' names.
    """
    from src.auth.tenants import onboard_family

    # Check if the message contains children's names
    # Simple heuristic: if message has comma-separated names or "and"-separated names
    message.lower().strip()

    # If they're providing names in response to onboarding prompt
    if any(sep in message for sep in [",", " and "]):
        # Parse names
        names = [
            n.strip()
            for n in message.replace(" and ", ",").split(",")
            if n.strip()
        ]
        if names:
            children_info = [{"name": name} for name in names]
            children = await onboard_family(session, family.id, children_info)
            child_names = ", ".join(c.name for c in children)
            return (
                f"Great! I've added {child_names} to your family. "
                "You're all set! Try telling me about an upcoming event."
            )

    # Check if it's a single name
    words = message.strip().split()
    if 1 <= len(words) <= 3 and all(w[0].isupper() for w in words if w):
        children_info = [{"name": message.strip()}]
        children = await onboard_family(session, family.id, children_info)
        return (
            f"Great! I've added {children[0].name} to your family. "
            "Do you have more kids to add? If not, you're all set — "
            "try telling me about an upcoming event."
        )

    return (
        "Welcome to Radar! I help families coordinate kids' activities. "
        "To get started, what are your kids' names?"
    )


def _is_ics_file(filename: str, mime_type: str) -> bool:
    """Check if a document is an ICS calendar file."""
    if filename.lower().endswith(".ics"):
        return True
    if mime_type in ("text/calendar", "application/ics"):
        return True
    return False


async def _download_whatsapp_media(media_id: str) -> str:
    """Download media content from Meta Cloud API.

    Two-step process:
    1. GET /{media_id} to get the download URL
    2. GET the download URL with auth header to get content

    Returns the file content as a string.
    Does not persist the file (per data model rules).
    """
    headers = {
        "Authorization": f"Bearer {settings.whatsapp_api_token.get_secret_value()}"
    }

    async with httpx.AsyncClient() as client:
        # Step 1: Get media URL
        resp = await client.get(
            f"https://graph.facebook.com/v21.0/{media_id}",
            headers=headers,
            timeout=10.0,
        )
        resp.raise_for_status()
        media_url = resp.json().get("url")

        if not media_url:
            raise ValueError(f"No URL in media response for {media_id}")

        # Step 2: Download content
        resp = await client.get(
            media_url,
            headers=headers,
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.text


async def _handle_ics_upload(
    session: AsyncSession, family, caregiver, document: dict
) -> str:
    """Handle an ICS file uploaded via WhatsApp."""
    from src.actions.whatsapp import send_buttons_to_family
    from src.ingestion.ics import process_ics_attachment
    from src.state.models import PendingActionType
    from src.state.pending import create_pending_action
    from src.utils.button_ids import encode_button_id

    media_id = document["media_id"]

    # Download file from Meta media API
    try:
        ics_content = await _download_whatsapp_media(media_id)
    except Exception:
        logger.exception("Failed to download ICS file (media_id=%s)", media_id)
        return "Sorry, I couldn't download that file. Please try again."

    # Process through ICS pipeline
    results = await process_ics_attachment(session, family.id, ics_content)

    if not results:
        return (
            "I couldn't find any events in that calendar file. "
            "Please make sure it's a valid .ics file."
        )

    new_events = [(event, is_new) for event, is_new in results if is_new]
    dup_count = len(results) - len(new_events)

    if not new_events:
        return f"I found {len(results)} event(s) in that file, but they're already on your calendar."

    # Build event summary for batch confirmation
    event_lines = []
    event_ids = []
    for event, _ in new_events:
        event_ids.append(str(event.id))
        time_str = event.datetime_start.strftime("%b %d, %I:%M %p") if event.datetime_start else "TBD"
        line = f"  \u2022 {event.title} \u2014 {time_str}"
        if event.location:
            line += f" ({event.location})"
        event_lines.append(line)

    # Create a single pending action for the whole batch
    pending = await create_pending_action(
        session,
        family_id=family.id,
        action_type=PendingActionType.event_confirmation,
        draft_content=f"{len(new_events)} events from {document.get('filename', 'calendar file')}",
        context={
            "event_ids": event_ids,
            "source": "ics_attachment",
            "filename": document.get("filename", ""),
            "batch": True,
        },
    )

    body = f"Found {len(new_events)} new event(s) in that calendar file:\n"
    body += "\n".join(event_lines)
    body += "\n\nAdd all to your calendar?"

    buttons = [
        {"id": encode_button_id("event_confirm", str(pending.id), "yes"), "title": "Yes, add all"},
        {"id": encode_button_id("event_confirm", str(pending.id), "no"), "title": "No, skip"},
    ]

    try:
        await send_buttons_to_family(session, family.id, body, buttons)
    except Exception:
        logger.exception("Failed to send ICS batch confirmation")

    summary = f"Found {len(new_events)} new event(s) in that calendar file."
    if dup_count > 0:
        summary += f" ({dup_count} already on your calendar.)"
    return summary
