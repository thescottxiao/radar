"""WhatsApp message ingestion: receives and processes inbound messages."""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

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

    # Look up family
    family = None
    if group_id:
        family = await families_dal.get_family_by_group_id(session, group_id)

    # If no group_id or family not found, try to find by phone
    if family is None:
        # For DMs, we need to find the family this caregiver belongs to
        # This is a simplification — in production we'd have a separate lookup
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
        if msg_type == "text":
            text = msg.get("text", {}).get("body", "")
        elif msg_type == "interactive":
            # Button replies or list replies
            interactive = msg.get("interactive", {})
            if "button_reply" in interactive:
                text = interactive["button_reply"].get("title", "")
            elif "list_reply" in interactive:
                text = interactive["list_reply"].get("title", "")
            else:
                return None
        else:
            # Audio, image, etc. — not handled in Phase 1
            # Voice notes will be handled in Phase 2
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
