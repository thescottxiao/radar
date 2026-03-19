"""ICS feed poller and differ.

Polls ICS feed subscriptions, diffs against stored events, and passes
new/changed events through the extraction pipeline.
"""

import logging
from datetime import UTC, datetime
from uuid import UUID

import httpx
from icalendar import Calendar
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.extraction.dedup import deduplicate_event
from src.extraction.email import ExtractedEvent
from src.state import events as event_dal
from src.state.models import Event, EventSource, IcsSubscription

logger = logging.getLogger(__name__)


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
    current_events = await parse_ics_feed(content)
    changes = await diff_ics_events(current_events, subscription.family_id, session)

    # Process changes through dedup
    for event_data in changes:
        extracted = ExtractedEvent(
            title=event_data["title"],
            event_type=event_data.get("event_type", "other"),
            datetime_start=event_data.get("datetime_start"),
            datetime_end=event_data.get("datetime_end"),
            location=event_data.get("location"),
            description=event_data.get("description"),
            confidence=0.9,  # ICS data is generally reliable
        )
        source_ref = event_data.get("uid", "")
        await deduplicate_event(
            session,
            subscription.family_id,
            extracted,
            source=EventSource.ics_feed,
            source_ref=source_ref,
        )

    logger.info(
        "Polled ICS feed %s (family %s): %d events parsed, %d changes",
        subscription.url,
        subscription.family_id,
        len(current_events),
        len(changes),
    )


async def parse_ics_feed(content: str) -> list[dict]:
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
