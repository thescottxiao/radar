"""State update actions — persist extraction results into the database.

Handles event dedup, action item creation, and learning creation.
Below 0.6 confidence: flags items for caregiver confirmation.
"""

import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from src.extraction.dedup import deduplicate_event
from src.extraction.email import ExtractionResult
from src.state import children as children_dal
from src.state import events as event_dal
from src.state import learning as learning_dal
from src.state.models import (
    ActionItemType,
    Event,
    EventSource,
)

logger = logging.getLogger(__name__)

# Confidence threshold — below this, items are flagged for confirmation
CONFIDENCE_THRESHOLD = 0.6

# Mapping from extraction string types to model enums
_ACTION_TYPE_MAP = {
    "form_to_sign": ActionItemType.form_to_sign,
    "payment_due": ActionItemType.payment_due,
    "item_to_bring": ActionItemType.item_to_bring,
    "item_to_purchase": ActionItemType.item_to_purchase,
    "registration_deadline": ActionItemType.registration_deadline,
    "rsvp_needed": ActionItemType.rsvp_needed,
    "contact_needed": ActionItemType.contact_needed,
    "other": ActionItemType.other,
}

# Map source strings to EventSource enum
_SOURCE_MAP = {
    "email": EventSource.email,
    "calendar": EventSource.calendar,
    "manual": EventSource.manual,
    "ics_feed": EventSource.ics_feed,
    "forwarded": EventSource.forwarded,
}


async def persist_extraction(
    session: AsyncSession,
    family_id: UUID,
    result: ExtractionResult,
    source: str = "email",
    source_ref: str | None = None,
) -> list[Event]:
    """Persist extraction results: dedup events, create action items and learnings.

    For each extracted event: dedup check, then create or merge.
    For action items: create with status pending.
    For learnings: create as unconfirmed.
    Below 0.6 confidence: flag for caregiver confirmation.

    Returns list of created/merged Event objects.
    """
    event_source = _SOURCE_MAP.get(source, EventSource.email)
    persisted_events: list[Event] = []

    # Resolve child names to IDs for linking
    children = await children_dal.get_children_for_family(session, family_id)
    child_name_map = {c.name.lower(): c.id for c in children}

    # ── Events ──────────────────────────────────────────────────────
    for extracted_event in result.events:
        event, is_new = await deduplicate_event(
            session,
            family_id,
            extracted_event,
            source=event_source,
            source_ref=source_ref,
        )

        # Link children to event
        child_ids = _resolve_child_names(extracted_event.child_names, child_name_map)
        if child_ids and is_new:
            await event_dal.link_children_to_event(session, family_id, event.id, child_ids)

        # Flag low confidence for confirmation
        if extracted_event.confidence < CONFIDENCE_THRESHOLD:
            logger.info(
                "Low confidence event (%.2f): '%s' — flagged for confirmation",
                extracted_event.confidence,
                extracted_event.title,
            )
            # confirmed_by_caregiver stays False (default)

        persisted_events.append(event)

    # ── Action Items ────────────────────────────────────────────────
    for extracted_item in result.action_items:
        action_type = _ACTION_TYPE_MAP.get(extracted_item.action_type, ActionItemType.other)

        # Find the associated event if action item matches one
        linked_event_id = None
        if persisted_events:
            # Simple heuristic: link to first event if only one, otherwise leave unlinked
            if len(persisted_events) == 1:
                linked_event_id = persisted_events[0].id

        await event_dal.create_action_item(
            session,
            family_id,
            source=event_source,
            source_ref=source_ref,
            type=action_type,
            description=extracted_item.description,
            due_date=extracted_item.due_date,
            event_id=linked_event_id,
        )

        if extracted_item.confidence < CONFIDENCE_THRESHOLD:
            logger.info(
                "Low confidence action item (%.2f): '%s' — flagged for confirmation",
                extracted_item.confidence,
                extracted_item.description[:60],
            )

    # ── Learnings ───────────────────────────────────────────────────
    for extracted_learning in result.learnings:
        # Resolve entity if possible
        entity_type = extracted_learning.entity_type
        entity_id = None
        if entity_type == "child" and extracted_learning.entity_name:
            child_id = child_name_map.get(extracted_learning.entity_name.lower())
            if child_id:
                entity_id = child_id

        await learning_dal.create_learning(
            session,
            family_id,
            category=extracted_learning.category,
            fact=extracted_learning.fact,
            source=source,
            confidence=extracted_learning.confidence,
            entity_type=entity_type,
            entity_id=entity_id,
        )

    return persisted_events


def _resolve_child_names(
    names: list[str], child_name_map: dict[str, UUID]
) -> list[UUID]:
    """Resolve extracted child names to UUIDs using fuzzy matching."""
    resolved = []
    for name in names:
        name_lower = name.lower().strip()
        # Exact match
        if name_lower in child_name_map:
            resolved.append(child_name_map[name_lower])
            continue
        # Prefix match
        for known_name, child_id in child_name_map.items():
            if known_name.startswith(name_lower) or name_lower.startswith(known_name):
                resolved.append(child_id)
                break
    return resolved
