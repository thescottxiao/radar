"""Calendar Change Detector — classifies and processes GCal event changes.

Handles:
  - new_event: dedup check, create if new
  - time_change: find matching Event, update
  - cancellation: mark event cancelled, log exception if recurring
  - location_change: update, notify if transport implications
"""

import logging
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from src.state import events as events_dal
from src.state.models import Event, EventSource, ScheduleException

logger = logging.getLogger(__name__)

# GCal status values
GCAL_STATUS_CANCELLED = "cancelled"
GCAL_STATUS_CONFIRMED = "confirmed"
GCAL_STATUS_TENTATIVE = "tentative"


# ── GCal event field mapping ───────────────────────────────────────────


def gcal_event_to_radar_event(
    gcal_event: dict,
    family_id: UUID,
    caregiver_id: UUID,
) -> dict:
    """Map GCal event fields to Radar Event model fields.

    Args:
        gcal_event: Raw GCal event dict from the Google Calendar API.
        family_id: The family this event belongs to.
        caregiver_id: The caregiver whose calendar this came from.

    Returns:
        Dict of kwargs suitable for events_dal.create_event().
    """
    gcal_id = gcal_event.get("id", "")
    summary = gcal_event.get("summary", "Untitled Event")
    description = gcal_event.get("description")
    location = gcal_event.get("location")

    # Parse datetime — GCal uses either dateTime or date (all-day)
    start_info = gcal_event.get("start", {})
    end_info = gcal_event.get("end", {})

    datetime_start = _parse_gcal_datetime(start_info)
    datetime_end = _parse_gcal_datetime(end_info)

    # Check for recurrence
    is_recurring = bool(gcal_event.get("recurringEventId"))

    return {
        "source": EventSource.calendar,
        "source_refs": [f"gcal:{gcal_id}"],
        "type": "other",
        "title": summary,
        "description": description,
        "datetime_start": datetime_start,
        "datetime_end": datetime_end,
        "location": location,
        "is_recurring": is_recurring,
        "confirmed_by_caregiver": True,  # Came from their own calendar
        "extraction_confidence": 1.0,  # Direct from calendar, no extraction needed
    }


async def process_calendar_change(
    session: AsyncSession,
    family_id: UUID,
    gcal_event: dict,
    caregiver_id: UUID,
) -> dict | None:
    """Classify and process a GCal event change.

    Args:
        session: Database session.
        family_id: The family this change belongs to.
        gcal_event: The changed GCal event dict.
        caregiver_id: The caregiver whose calendar produced this change.

    Returns:
        A dict with keys:
            - change_type: "new_event" | "time_change" | "location_change" | "cancellation" | "no_action"
            - event_id: UUID of the created/updated Radar event (if applicable)
            - notification: str notification text (if the change warrants one)
        Or None if no action needed.
    """
    gcal_id = gcal_event.get("id", "")
    status = gcal_event.get("status", "")
    summary = gcal_event.get("summary", "Untitled Event")

    logger.info(
        "Processing calendar change: gcal_id=%s summary=%s status=%s",
        gcal_id,
        summary,
        status,
    )

    # Handle cancellation
    if status == GCAL_STATUS_CANCELLED:
        return await _handle_cancellation(session, family_id, gcal_id, summary)

    # Look for existing Radar event with this GCal ID
    existing_events = await events_dal.get_events_by_source_ref(
        session, family_id, f"gcal:{gcal_id}"
    )

    if existing_events:
        # This is an update to an existing event
        return await _handle_update(
            session, family_id, existing_events[0], gcal_event
        )
    else:
        # This might be a new event — check for duplicates first
        return await _handle_new_event(
            session, family_id, gcal_event, caregiver_id
        )


# ── Change type handlers ───────────────────────────────────────────────


async def _handle_cancellation(
    session: AsyncSession,
    family_id: UUID,
    gcal_id: str,
    summary: str,
) -> dict:
    """Handle a cancelled GCal event."""
    existing = await events_dal.get_events_by_source_ref(
        session, family_id, f"gcal:{gcal_id}"
    )

    if not existing:
        logger.info("Cancellation for unknown event gcal:%s — ignoring", gcal_id)
        return {"change_type": "no_action", "event_id": None, "notification": None}

    event = existing[0]

    # If this is part of a recurring schedule, log an exception instead of deleting
    if event.is_recurring and event.recurring_schedule_id:
        exception = ScheduleException(
            recurring_schedule_id=event.recurring_schedule_id,
            family_id=family_id,
            original_date=event.datetime_start.date(),
            exception_type="cancelled",
            reason="Cancelled in Google Calendar",
        )
        session.add(exception)

    # Soft-delete by marking as cancelled (update description)
    await events_dal.update_event(
        session,
        family_id,
        event.id,
        description=(event.description or "") + "\n[CANCELLED]",
    )

    # No WhatsApp notification — importing GCal change into authoritative local DB.
    # The user already knows about changes they made directly in their calendar.
    return {
        "change_type": "cancellation",
        "event_id": event.id,
        "notification": None,
    }


async def _handle_update(
    session: AsyncSession,
    family_id: UUID,
    existing_event: Event,
    gcal_event: dict,
) -> dict:
    """Handle an update to an existing Radar event from GCal."""
    changes: list[str] = []
    update_kwargs: dict = {}

    # Check for time change
    start_info = gcal_event.get("start", {})
    new_start = _parse_gcal_datetime(start_info)
    if new_start and new_start != existing_event.datetime_start:
        update_kwargs["datetime_start"] = new_start
        changes.append(
            f"time changed to {new_start.strftime('%A, %B %d at %I:%M %p')}"
        )

    end_info = gcal_event.get("end", {})
    new_end = _parse_gcal_datetime(end_info)
    if new_end and new_end != existing_event.datetime_end:
        update_kwargs["datetime_end"] = new_end

    # Check for location change
    new_location = gcal_event.get("location")
    if new_location and new_location != existing_event.location:
        update_kwargs["location"] = new_location
        changes.append(f"location changed to {new_location}")

    # Check for title change
    new_summary = gcal_event.get("summary", "")
    if new_summary and new_summary != existing_event.title:
        update_kwargs["title"] = new_summary
        changes.append(f"renamed to \"{new_summary}\"")

    # Check for description change
    new_description = gcal_event.get("description")
    if new_description and new_description != existing_event.description:
        update_kwargs["description"] = new_description

    if not update_kwargs:
        return {"change_type": "no_action", "event_id": existing_event.id, "notification": None}

    # Apply updates
    await events_dal.update_event(
        session, family_id, existing_event.id, **update_kwargs
    )

    # Determine primary change type for classification
    change_type = "time_change" if "datetime_start" in update_kwargs else "location_change"

    # No WhatsApp notification — importing GCal change into authoritative local DB.
    # The user already knows about changes they made directly in their calendar.
    return {
        "change_type": change_type,
        "event_id": existing_event.id,
        "notification": None,
    }


async def _handle_new_event(
    session: AsyncSession,
    family_id: UUID,
    gcal_event: dict,
    caregiver_id: UUID,
) -> dict:
    """Handle a new GCal event — check dedup, create if new."""
    mapped = gcal_event_to_radar_event(gcal_event, family_id, caregiver_id)

    # Dedup check: ±30 min AND title similarity > 0.7
    duplicate = await events_dal.find_duplicate_event(
        session,
        family_id,
        mapped["title"],
        mapped["datetime_start"],
    )

    if duplicate:
        # Update the existing event with the GCal source ref so future
        # updates from GCal are linked
        gcal_id = gcal_event.get("id", "")
        existing_refs = duplicate.source_refs or []
        new_ref = f"gcal:{gcal_id}"
        if new_ref not in existing_refs:
            await events_dal.update_event(
                session,
                family_id,
                duplicate.id,
                source_refs=existing_refs + [new_ref],
            )
        logger.info(
            "Dedup: GCal event '%s' matches existing event %s",
            mapped["title"],
            duplicate.id,
        )
        return {
            "change_type": "no_action",
            "event_id": duplicate.id,
            "notification": None,
        }

    # Create new event
    event = await events_dal.create_event(session, family_id, **mapped)

    # No WhatsApp notification — GCal is the source of truth, the user
    # already knows about events they added directly to their calendar.
    # Email-extracted events go through a separate notification path.
    return {
        "change_type": "new_event",
        "event_id": event.id,
        "notification": None,
    }


# ── Utility functions ──────────────────────────────────────────────────


def _parse_gcal_datetime(dt_info: dict) -> datetime | None:
    """Parse a GCal datetime dict to a Python datetime.

    GCal uses either:
      - {"dateTime": "2026-03-25T16:00:00-04:00"} for timed events
      - {"date": "2026-03-25"} for all-day events
    """
    if not dt_info:
        return None

    if "dateTime" in dt_info:
        return datetime.fromisoformat(dt_info["dateTime"])

    if "date" in dt_info:
        # All-day event — use midnight UTC
        return datetime.fromisoformat(dt_info["date"] + "T00:00:00+00:00")

    return None


