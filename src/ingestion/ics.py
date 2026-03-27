"""ICS feed poller, differ, and attachment processor.

Polls ICS feed subscriptions, diffs against stored events, and passes
new/changed events through the extraction pipeline. Also handles ICS
file attachments from WhatsApp uploads and email.
"""

import logging
from datetime import UTC, datetime
from uuid import UUID

import httpx
from icalendar import Calendar
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.actions.whatsapp import send_buttons_to_family
from src.utils.timezone import fmt_dt
from src.extraction.dedup import deduplicate_event
from src.extraction.email import ExtractedEvent
from src.state import events as event_dal
from src.state.models import Event, EventSource, IcsSubscription, PendingActionType
from src.state.pending import create_pending_action
from src.utils.button_ids import encode_button_id

logger = logging.getLogger(__name__)


MAX_ICS_SIZE = 1_000_000  # 1 MB
ICS_MIME_TYPES = {"text/calendar", "application/ics"}


def is_ics_file(filename: str, mime_type: str) -> bool:
    """Check if a file is an ICS calendar file by extension or MIME type."""
    return filename.lower().endswith(".ics") or mime_type in ICS_MIME_TYPES


async def process_ics_attachment(
    session: AsyncSession,
    family_id: UUID,
    content: str,
) -> list[tuple[Event, bool]]:
    """Parse an ICS file attachment and run events through dedup.

    Shared entry point for WhatsApp uploads and email attachments.
    Returns list of (Event, is_new) tuples for the caller to confirm.
    """
    # Validate content
    if len(content) > MAX_ICS_SIZE:
        logger.warning("ICS content too large (%d bytes), skipping", len(content))
        return []

    if not content.strip().upper().startswith("BEGIN:VCALENDAR"):
        logger.info("Content does not look like ICS (no BEGIN:VCALENDAR)")
        return []

    parsed_events = parse_ics_feed(content)
    if not parsed_events:
        return []

    results: list[tuple[Event, bool]] = []
    for event_data in parsed_events:
        result = await _dedup_ics_event(session, family_id, event_data)
        if result is not None:
            results.append(result)

    logger.info(
        "Processed ICS attachment for family %s: %d parsed, %d results",
        family_id,
        len(parsed_events),
        len(results),
    )
    return results


async def poll_ics_feeds(session: AsyncSession) -> None:
    """Fetch all active ICS subscriptions and process changes.

    Uses ETag for conditional fetching to avoid re-processing unchanged feeds.
    """
    result = await session.execute(
        select(IcsSubscription).where(IcsSubscription.is_active.is_(True))
    )
    subscriptions = list(result.scalars().all())

    if not subscriptions:
        logger.info("No active ICS subscriptions to poll")
        return

    for sub in subscriptions:
        try:
            await _poll_single_feed(session, sub)
        except Exception:
            logger.exception(
                "Error polling ICS feed %s (family %s)", sub.url, sub.family_id
            )


async def _poll_single_feed(
    session: AsyncSession, subscription: IcsSubscription
) -> None:
    """Poll a single ICS feed with conditional ETag check."""
    headers = {}
    if subscription.last_etag:
        headers["If-None-Match"] = subscription.last_etag

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            subscription.url,
            headers=headers,
            timeout=30.0,
            follow_redirects=True,
        )

    # 304 Not Modified — no changes
    if resp.status_code == 304:
        logger.debug("ICS feed unchanged: %s", subscription.url)
        subscription.last_polled_at = datetime.now(UTC)
        await session.flush()
        return

    resp.raise_for_status()

    # Update ETag and poll timestamp
    new_etag = resp.headers.get("ETag")
    if new_etag:
        subscription.last_etag = new_etag
    subscription.last_polled_at = datetime.now(UTC)
    await session.flush()

    # Parse and diff
    content = resp.text
    current_events = parse_ics_feed(content)
    changes = await diff_ics_events(current_events, subscription.family_id, session)

    # Process changes through dedup
    for event_data in changes:
        await _dedup_ics_event(session, subscription.family_id, event_data)

    logger.info(
        "Polled ICS feed %s (family %s): %d events parsed, %d changes",
        subscription.url,
        subscription.family_id,
        len(current_events),
        len(changes),
    )


async def send_ics_batch_confirmation(
    session: AsyncSession,
    family_id: UUID,
    new_events: list[Event],
    filename: str,
    source_label: str,
    extra_context: dict | None = None,
) -> None:
    """Send a single batch confirmation for ICS events via WhatsApp buttons.

    Shared by all ingestion channels (WhatsApp, Gmail, forward-to email).
    """
    from src.state import families as families_dal
    _family = await families_dal.get_family(session, family_id)
    _family_tz = _family.timezone if _family else "America/New_York"

    event_lines = []
    event_ids = []
    for event in new_events:
        event_ids.append(str(event.id))
        time_str = fmt_dt(event.datetime_start, _family_tz, "%b %d, %I:%M %p")
        line = f"  \u2022 {event.title} \u2014 {time_str}"
        if event.location:
            line += f" ({event.location})"
        event_lines.append(line)

    context = {
        "event_ids": event_ids,
        "source": "ics_attachment",
        "filename": filename,
        "batch": True,
    }
    if extra_context:
        context.update(extra_context)

    pending = await create_pending_action(
        session,
        family_id=family_id,
        action_type=PendingActionType.event_confirmation,
        draft_content=f"{len(new_events)} events from {source_label}",
        context=context,
    )

    body = f"Found {len(new_events)} new event(s) from {source_label}:\n"
    body += "\n".join(event_lines)
    body += "\n\nAdd all to your calendar?"

    buttons = [
        {"id": encode_button_id("event_confirm", str(pending.id), "yes"), "title": "Yes, add all"},
        {"id": encode_button_id("event_confirm", str(pending.id), "no"), "title": "No, skip"},
    ]

    await send_buttons_to_family(session, family_id, body, buttons)


async def _dedup_ics_event(
    session: AsyncSession, family_id: UUID, event_data: dict
) -> tuple[Event, bool] | None:
    """Convert an ICS event dict to ExtractedEvent and run through dedup."""
    if not event_data.get("datetime_start"):
        return None

    extracted = ExtractedEvent(
        title=event_data["title"],
        event_type=event_data.get("event_type", "other"),
        datetime_start=event_data.get("datetime_start"),
        datetime_end=event_data.get("datetime_end"),
        location=event_data.get("location"),
        description=event_data.get("description"),
        confidence=0.9,
    )
    source_ref = event_data.get("uid", "")
    try:
        return await deduplicate_event(
            session,
            family_id,
            extracted,
            source=EventSource.ics_feed,
            source_ref=source_ref,
        )
    except ValueError:
        logger.warning(
            "Skipping ICS event '%s' — missing required fields",
            event_data.get("title"),
        )
        return None


def parse_ics_feed(content: str) -> list[dict]:
    """Parse an ICS feed into a list of event dictionaries.

    Returns list of dicts with keys: uid, title, datetime_start, datetime_end,
    location, description.
    """
    events = []

    try:
        cal = Calendar.from_ical(content)
    except Exception as e:
        logger.error("Failed to parse ICS content: %s", e)
        return []

    for component in cal.walk():
        if component.name != "VEVENT":
            continue

        uid = str(component.get("uid", ""))
        summary = str(component.get("summary", ""))
        if not summary:
            continue

        dtstart = component.get("dtstart")
        dtend = component.get("dtend")
        location = component.get("location")
        description = component.get("description")

        # Convert to datetime
        start_dt = _ical_to_datetime(dtstart) if dtstart else None
        end_dt = _ical_to_datetime(dtend) if dtend else None

        events.append({
            "uid": uid,
            "title": summary,
            "datetime_start": start_dt,
            "datetime_end": end_dt,
            "location": str(location) if location else None,
            "description": str(description) if description else None,
        })

    return events


async def diff_ics_events(
    current_events: list[dict],
    family_id: UUID,
    session: AsyncSession,
) -> list[dict]:
    """Diff ICS events against stored events to find new or changed ones.

    An event is considered new if no existing event matches its source_ref (UID).
    An event is considered changed if the UID matches but title or datetime differ.

    Returns list of event dicts that are new or changed.
    """
    changes = []

    for ics_event in current_events:
        uid = ics_event.get("uid", "")
        if not uid:
            # No UID — treat as new, let dedup handle it
            changes.append(ics_event)
            continue

        # Check if we already have this event by source_ref
        existing = await event_dal.get_events_by_source_ref(session, family_id, uid)

        if not existing:
            # New event
            changes.append(ics_event)
        else:
            # Check if changed
            stored = existing[0]
            if _event_changed(stored, ics_event):
                changes.append(ics_event)

    return changes


def _event_changed(stored: "Event", ics_event: dict) -> bool:
    """Check if an ICS event has changed relative to the stored version."""
    if stored.title != ics_event.get("title", ""):
        return True

    ics_start = ics_event.get("datetime_start")
    if ics_start and stored.datetime_start:
        # Compare with some tolerance (1 minute)
        diff = abs((stored.datetime_start - ics_start).total_seconds())
        if diff > 60:
            return True

    ics_location = ics_event.get("location")
    if ics_location and stored.location and stored.location != ics_location:
        return True

    return False


def _ical_to_datetime(dt_prop) -> datetime | None:
    """Convert an icalendar date/datetime property to a timezone-aware datetime."""
    if dt_prop is None:
        return None

    dt = dt_prop.dt
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt
    # date (not datetime) — convert to midnight UTC
    if hasattr(dt, "year"):
        return datetime(dt.year, dt.month, dt.day, tzinfo=UTC)
    return None
