"""Shared family context builder for agent prompts.

All agents that need family context (calendar, reminders, extraction, etc.)
should call build_family_context() to get a consistent context dict that
includes children, caregivers, upcoming events, confirmed learnings, and
active preferences.
"""

import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from src.state import children as children_dal
from src.state import events as events_dal
from src.state import families as families_dal
from src.state import learning as learning_dal
from src.state import preferences as pref_dal
from src.state.models import CaregiverPreferences, FamilyLearning

logger = logging.getLogger(__name__)


async def build_family_context(
    session: AsyncSession,
    family_id: UUID,
    caregiver_id: UUID | None = None,
) -> dict:
    """Build context dict for LLM prompts, including learnings and preferences.

    Args:
        session: DB session
        family_id: The family tenant
        caregiver_id: If provided, includes per-caregiver preferences merged
                      with family-wide ones
    """
    family = await families_dal.get_family(session, family_id)
    if not family:
        raise ValueError(f"Family {family_id} not found")

    kids = await children_dal.get_children_for_family(session, family_id)
    caregivers = await families_dal.get_caregivers_for_family(session, family_id)
    upcoming = await events_dal.get_upcoming_events(session, family_id, days=14)

    # Build children info
    children_info = []
    for child in kids:
        info = child.name
        parts = []
        if child.school:
            parts.append(f"school: {child.school}")
        if child.activities:
            parts.append(f"activities: {', '.join(child.activities)}")
        if parts:
            info += f" ({', '.join(parts)})"
        children_info.append(info)

    caregiver_info = [c.name or c.whatsapp_phone for c in caregivers]

    # Build events text
    event_lines = []
    for ev in upcoming:
        start_str = ev.datetime_start.strftime("%a %b %d at %I:%M %p")
        line = f"- {ev.title}: {start_str}"
        if ev.location:
            line += f" at {ev.location}"
        event_lines.append(line)

    today = datetime.now(UTC).strftime("%Y-%m-%d")

    # Confirmed learnings (non-preference facts that haven't graduated)
    learnings = []
    preferences = []
    try:
        learnings = await learning_dal.get_confirmed_learnings(session, family_id)
        preferences = await learning_dal.get_active_preferences(
            session, family_id, caregiver_id
        )
    except Exception:
        logger.debug("Could not load learnings/preferences for family %s", family_id)
    learnings_text = _format_learnings(learnings)
    preferences_text = _format_preferences(preferences)

    # Structured preferences (optional — table may not exist yet)
    structured_prefs = None
    if caregiver_id:
        try:
            structured_prefs = await pref_dal.get_or_create_preferences(
                session, caregiver_id, family_id
            )
        except Exception:
            logger.debug("Could not load structured preferences for caregiver %s", caregiver_id)

    # Build full context string
    sections = [
        f"Children: {', '.join(children_info) if children_info else 'none yet'}",
        f"Caregivers: {', '.join(caregiver_info)}",
        f"Upcoming events:\n" + ("\n".join(event_lines) if event_lines else "  (none)"),
    ]
    if learnings_text:
        sections.append(f"Known facts about this family:\n{learnings_text}")
    if preferences_text:
        sections.append(f"Family preferences:\n{preferences_text}")

    family_context = "\n".join(sections)

    return {
        "family": family,
        "children": kids,
        "caregivers": caregivers,
        "upcoming": upcoming,
        "children_names": [c.name for c in kids],
        "caregiver_names": caregiver_info,
        "family_context": family_context,
        "today": today,
        "timezone": family.timezone,
        "learnings": learnings,
        "preferences": preferences,
        "structured_prefs": structured_prefs,
        "learnings_text": learnings_text,
        "preferences_text": preferences_text,
    }


def _format_learnings(learnings: list[FamilyLearning]) -> str:
    """Format confirmed learnings for prompt injection."""
    if not learnings:
        return ""
    # Filter out preference categories — those go in preferences_text
    facts = [
        le for le in learnings
        if not le.category.startswith("pref_")
    ]
    if not facts:
        return ""
    lines = [f"- {le.fact}" for le in facts]
    return "\n".join(lines)


def _format_preferences(preferences: list[FamilyLearning]) -> str:
    """Format active freeform preferences for prompt injection."""
    if not preferences:
        return ""
    lines = [f"- {pref.fact}" for pref in preferences]
    return "\n".join(lines)
