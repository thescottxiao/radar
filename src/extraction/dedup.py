"""Event deduplication — fuzzy matching and merge logic.

Dedup uses datetime +-30 min AND title similarity > 0.7.
Prefers false negatives (miss a dup) over false positives (incorrectly merge distinct events).
"""

import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from src.extraction.email import ExtractedEvent
from src.state import events as event_dal
from src.state.models import Event, EventSource, EventType

logger = logging.getLogger(__name__)

# Mapping from extraction string types to model enums
_EVENT_TYPE_MAP = {
    "birthday_party": EventType.birthday_party,
    "sports_practice": EventType.sports_practice,
    "sports_game": EventType.sports_game,
    "school_event": EventType.school_event,
    "camp": EventType.camp,
    "playdate": EventType.playdate,
    "medical_appointment": EventType.medical_appointment,
    "dental_appointment": EventType.dental_appointment,
    "recital_performance": EventType.recital_performance,
    "registration_deadline": EventType.registration_deadline,
    "other": EventType.other,
}


def _resolve_event_type(type_str: str) -> EventType:
    return _EVENT_TYPE_MAP.get(type_str, EventType.other)


async def deduplicate_event(
    session: AsyncSession,
    family_id: UUID,
    extracted_event: ExtractedEvent,
    source: EventSource = EventSource.email,
    source_ref: str | None = None,
) -> tuple[Event, bool]:
    """Check for duplicate event and merge or create.

    Returns (event, is_new) where is_new is True if a new event was created,
    False if an existing event was enriched/merged.
    """
    if extracted_event.datetime_start is None:
        # Cannot dedup without a start time; always create new
        event = await event_dal.create_event(
            session,
            family_id,
            source=source,
            source_refs=[source_ref] if source_ref else [],
            type=_resolve_event_type(extracted_event.event_type),
            title=extracted_event.title,
            description=extracted_event.description,
            location=extracted_event.location,
            extraction_confidence=extracted_event.confidence,
        )
        logger.info("Created event (no datetime for dedup): %s", event.id)
        return event, True

    # Look for duplicate
    existing = await event_dal.find_duplicate_event(
        session,
        family_id,
        title=extracted_event.title,
        datetime_start=extracted_event.datetime_start,
    )

    if existing is not None:
        # Merge: enrich existing event with any new information
        merged = await _merge_event(session, family_id, existing, extracted_event, source_ref)
        logger.info(
            "Merged event %s with extracted '%s'",
            merged.id,
            extracted_event.title,
        )
        return merged, False

    # No duplicate found — create new event
    from src.state.models import RsvpMethod, RsvpStatus

    rsvp_status = RsvpStatus.pending if extracted_event.rsvp_needed else RsvpStatus.not_applicable
    rsvp_method = RsvpMethod.not_applicable
    if extracted_event.rsvp_method:
        try:
            rsvp_method = RsvpMethod(extracted_event.rsvp_method)
        except ValueError:
            rsvp_method = RsvpMethod.not_applicable

    event = await event_dal.create_event(
        session,
        family_id,
        source=source,
        source_refs=[source_ref] if source_ref else [],
        type=_resolve_event_type(extracted_event.event_type),
        title=extracted_event.title,
        description=extracted_event.description,
        datetime_start=extracted_event.datetime_start,
        datetime_end=extracted_event.datetime_end,
        location=extracted_event.location,
        rsvp_status=rsvp_status,
        rsvp_deadline=extracted_event.rsvp_deadline,
        rsvp_method=rsvp_method,
        rsvp_contact=extracted_event.rsvp_contact,
        extraction_confidence=extracted_event.confidence,
    )
    logger.info("Created new event: %s '%s'", event.id, event.title)
    return event, True


async def _merge_event(
    session: AsyncSession,
    family_id: UUID,
    existing: Event,
    extracted: ExtractedEvent,
    source_ref: str | None,
) -> Event:
    """Merge extracted data into an existing event, enriching missing fields."""
    updates: dict = {}

    # Add source ref if new
    if source_ref:
        current_refs = existing.source_refs or []
        if source_ref not in current_refs:
            updates["source_refs"] = current_refs + [source_ref]

    # Enrich missing fields — only fill in what the existing event lacks
    if not existing.description and extracted.description:
        updates["description"] = extracted.description
    if not existing.location and extracted.location:
        updates["location"] = extracted.location
    if not existing.datetime_end and extracted.datetime_end:
        updates["datetime_end"] = extracted.datetime_end

    # Enrich RSVP info if not already set
    from src.state.models import RsvpStatus

    if existing.rsvp_status == RsvpStatus.not_applicable and extracted.rsvp_needed:
        updates["rsvp_status"] = RsvpStatus.pending
        if extracted.rsvp_deadline:
            updates["rsvp_deadline"] = extracted.rsvp_deadline
        if extracted.rsvp_contact:
            updates["rsvp_contact"] = extracted.rsvp_contact

    # Update confidence if new extraction is more confident
    if extracted.confidence and (
        existing.extraction_confidence is None
        or extracted.confidence > existing.extraction_confidence
    ):
        updates["extraction_confidence"] = extracted.confidence

    if updates:
        return await event_dal.update_event(session, family_id, existing.id, **updates)
    return existing
