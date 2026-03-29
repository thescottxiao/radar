"""Forward-to email handler.

Handles emails forwarded to family-{id}@radar.app addresses.
Parses family_id from the to address, verifies sender, and passes to extraction.
"""

import base64
import logging
import re
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from src.actions.state import persist_extraction
from src.extraction.email import process_email
from src.ingestion.ics import is_ics_file
from src.ingestion.schemas import EmailContent
from src.state import families as families_dal

logger = logging.getLogger(__name__)

# Pattern: family-{uuid}@radar.app (or configured domain)
_FORWARD_EMAIL_PATTERN = re.compile(
    r"family-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})@",
    re.IGNORECASE,
)


def parse_family_id_from_email(to_address: str) -> UUID | None:
    """Extract family UUID from a forward-to email address.

    Expected format: family-{uuid}@radar.app
    Returns None if the address doesn't match the expected pattern.
    """
    match = _FORWARD_EMAIL_PATTERN.search(to_address)
    if match:
        try:
            return UUID(match.group(1))
        except ValueError:
            return None
    return None


async def handle_forwarded_email(
    session: AsyncSession, payload: dict
) -> None:
    """Process a forwarded email.

    1. Parse family_id from the to address.
    2. Verify sender is a known caregiver (or process with lower confidence).
    3. Pass to the extraction pipeline.
    4. Persist extraction results.

    Args:
        session: Database session.
        payload: Parsed email payload with keys: to, from, subject, text, html,
                 message_id, date.
    """
    to_address = payload.get("to", "")
    from_address = payload.get("from", "")

    # Parse family_id from to address
    family_id = parse_family_id_from_email(to_address)
    if family_id is None:
        logger.warning(
            "Could not parse family_id from to address: %s", to_address
        )
        return

    # Verify family exists
    family = await families_dal.get_family(session, family_id)
    if family is None:
        logger.warning("Family not found for forward email: %s", family_id)
        return

    # Check if sender is a known caregiver
    caregiver = await families_dal.get_caregiver_by_email(session, from_address)
    is_known_sender = caregiver is not None and caregiver.family_id == family_id

    if not is_known_sender:
        logger.info(
            "Forward from unknown sender %s for family %s — processing with lower confidence",
            from_address,
            family_id,
        )

    # Build EmailContent
    email = EmailContent(
        message_id=payload.get("message_id", f"fwd-{family_id}-{hash(payload.get('subject', ''))}"),
        from_address=from_address,
        to_addresses=[to_address],
        subject=payload.get("subject", ""),
        body_text=payload.get("text", ""),
        body_html=payload.get("html", ""),
        date=payload.get("date"),
    )

    # Process ICS attachments if present
    await _process_forwarded_ics_attachments(
        session, family_id, payload.get("attachments", [])
    )

    # Process through extraction pipeline
    result = await process_email(
        session, family_id, email, source="forwarded"
    )

    if not result.is_relevant:
        logger.info(
            "Forwarded email rejected by triage: subject=%s", email.subject[:50]
        )
        return

    # If unknown sender, reduce confidence on all extracted items
    if not is_known_sender:
        for event in result.events:
            event.confidence = min(event.confidence, 0.5)
        for item in result.todos:
            item.confidence = min(item.confidence, 0.5)

    # Persist results
    await persist_extraction(
        session,
        family_id,
        result,
        source="forwarded",
        source_ref=email.message_id,
    )

    logger.info(
        "Processed forwarded email: family=%s subject=%s events=%d items=%d",
        family_id,
        email.subject[:50],
        len(result.events),
        len(result.todos),
    )


async def _process_forwarded_ics_attachments(
    session: AsyncSession,
    family_id: UUID,
    raw_attachments: list[dict],
) -> None:
    """Process ICS attachments from a forwarded email payload."""
    from src.ingestion.ics import process_ics_attachment, send_ics_batch_confirmation

    for att in raw_attachments:
        filename = att.get("filename", att.get("name", ""))
        content_type = att.get("content_type", att.get("type", ""))

        if not is_ics_file(filename, content_type):
            continue

        att_content = att.get("content", "")
        if not att_content:
            continue

        # Some inbound email services base64-encode attachment content
        if att.get("content_transfer_encoding") == "base64" or att.get("base64"):
            try:
                att_content = base64.b64decode(att_content).decode("utf-8", errors="replace")
            except Exception:
                logger.warning("Failed to base64-decode ICS attachment '%s'", filename)
                continue

        try:
            results = await process_ics_attachment(session, family_id, att_content)
        except Exception:
            logger.exception("Failed to process ICS attachment '%s' from forwarded email", filename)
            continue

        if not results:
            continue

        new_events = [event for event, is_new in results if is_new]
        if not new_events:
            continue

        try:
            await send_ics_batch_confirmation(
                session, family_id, new_events, filename,
                source_label=f"a forwarded email ({filename})",
            )
        except Exception:
            logger.exception("Failed to send ICS batch confirmation for '%s'", filename)

        logger.info(
            "Processed ICS attachment '%s' from forwarded email: family=%s, %d new events",
            filename, family_id, len(new_events),
        )
