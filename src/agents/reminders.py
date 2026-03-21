"""Reminder Engine — daily digests, weekly summaries, and immediate triggers.

Generates contextual, family-specific notifications using Sonnet for formatting.
"""

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from src.llm import generate
from src.state import events as event_dal
from src.state import learning as learning_dal

logger = logging.getLogger(__name__)


# ── Daily Digest ──────────────────────────────────────────────────────


async def generate_daily_digest(
    session: AsyncSession, family_id: UUID
) -> str | None:
    """Generate a daily digest for a family.

    Includes:
    - Today's events
    - Approaching deadlines (action items due within 48h)
    - Unclaimed transport (events with no drop_off_by or pick_up_by assigned)

    Returns None if nothing actionable (skip sending).
    Uses Sonnet to format into a friendly WhatsApp message.
    """
    now = datetime.now(UTC)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start + timedelta(days=1)

    # Today's events
    todays_events = await event_dal.get_events_in_range(
        session, family_id, today_start, today_end
    )

    # Approaching deadlines (action items due within 48h)
    upcoming_deadlines = await event_dal.get_action_items_due_soon(
        session, family_id, within_hours=48
    )

    # Unclaimed transport — events in next 48h with no transport assigned
    upcoming_events = await event_dal.get_events_in_range(
        session, family_id, now, now + timedelta(hours=48)
    )
    unclaimed_transport = [
        e for e in upcoming_events
        if e.drop_off_by is None or e.pick_up_by is None
    ]

    # Skip if nothing actionable
    if not todays_events and not upcoming_deadlines and not unclaimed_transport:
        logger.info("No actionable items for daily digest (family %s)", family_id)
        return None

    # Build context for LLM
    events_text = ""
    if todays_events:
        lines = []
        for e in todays_events:
            time_str = e.datetime_start.strftime("%I:%M %p") if e.datetime_start else "TBD"
            end_str = f" - {e.datetime_end.strftime('%I:%M %p')}" if e.datetime_end else ""
            location_str = f" at {e.location}" if e.location else ""
            lines.append(f"- {time_str}{end_str}: {e.title}{location_str}")
        events_text = "Today's events:\n" + "\n".join(lines)

    deadlines_text = ""
    if upcoming_deadlines:
        lines = []
        for ai in upcoming_deadlines:
            due_str = ai.due_date.strftime("%a %I:%M %p") if ai.due_date else "soon"
            lines.append(f"- {ai.description} (due {due_str})")
        deadlines_text = "Approaching deadlines:\n" + "\n".join(lines)

    transport_text = ""
    if unclaimed_transport:
        lines = []
        for e in unclaimed_transport:
            time_str = e.datetime_start.strftime("%I:%M %p") if e.datetime_start else "TBD"
            needs = []
            if e.drop_off_by is None:
                needs.append("drop-off")
            if e.pick_up_by is None:
                needs.append("pick-up")
            lines.append(f"- {e.title} at {time_str}: needs {' and '.join(needs)}")
        transport_text = "Transport needed:\n" + "\n".join(lines)

    sections = [s for s in [events_text, deadlines_text, transport_text] if s]
    context = "\n\n".join(sections)

    prompt = f"""\
Format the following daily digest information into a friendly, concise WhatsApp
message for a busy parent. Use short lines and emojis sparingly. Keep it scannable.
Do not add any events or information not in the data below.

{context}"""

    digest = await generate(
        prompt=prompt,
        system="You are Radar, a friendly family activity coordinator. "
        "Format daily digest messages for WhatsApp. Keep messages concise and actionable. "
        "Use minimal emojis. Do not invent information.",
    )

    logger.info("Generated daily digest for family %s", family_id)
    return digest.strip()


# ── Weekly Summary ────────────────────────────────────────────────────


async def generate_weekly_summary(
    session: AsyncSession, family_id: UUID
) -> str:
    """Generate a weekly summary for a family.

    Includes:
    - Week ahead events
    - Unsurfaced FamilyLearning entries (marks them as surfaced)
    - Prep status for upcoming events
    - Auto-confirms previously surfaced learnings (confirmation lifecycle)
    - Triggers graduation for newly confirmed learnings

    Always generates (never returns None).
    """
    now = datetime.now(UTC)
    week_end = now + timedelta(days=7)

    # ── Confirmation lifecycle ─────────────────────────────────────────
    # Auto-confirm learnings surfaced in the PREVIOUS summary cycle
    # that haven't been corrected since.
    newly_confirmed_ids = await learning_dal.auto_confirm_previously_surfaced(
        session, family_id
    )
    if newly_confirmed_ids:
        logger.info(
            "Auto-confirmed %d learnings for family %s",
            len(newly_confirmed_ids),
            family_id,
        )
        # Trigger graduation for newly confirmed learnings
        await _graduate_confirmed_learnings(session, family_id, newly_confirmed_ids)

    # ── Gather summary data ────────────────────────────────────────────

    # Week ahead events
    week_events = await event_dal.get_events_in_range(
        session, family_id, now, week_end
    )

    # Unsurfaced learnings (new this cycle)
    unsurfaced = await learning_dal.get_unsurfaced_learnings(session, family_id)

    # Events needing RSVP
    rsvp_events = await event_dal.get_events_needing_rsvp(session, family_id)

    # Build context
    events_text = ""
    if week_events:
        lines = []
        for e in week_events:
            date_str = e.datetime_start.strftime("%a %b %d, %I:%M %p") if e.datetime_start else "TBD"
            location_str = f" at {e.location}" if e.location else ""
            lines.append(f"- {date_str}: {e.title}{location_str}")
        events_text = "This week's events:\n" + "\n".join(lines)
    else:
        events_text = "This week's events:\nNo events scheduled."

    learnings_text = ""
    if unsurfaced:
        lines = [f"- {le.fact} ({le.category})" for le in unsurfaced]
        learnings_text = (
            "Things I've learned about your family (please correct if wrong):\n"
            + "\n".join(lines)
        )

    rsvp_text = ""
    if rsvp_events:
        lines = []
        for e in rsvp_events:
            deadline_str = (
                e.rsvp_deadline.strftime("%a %b %d") if e.rsvp_deadline else "no deadline set"
            )
            lines.append(f"- {e.title}: RSVP by {deadline_str}")
        rsvp_text = "RSVPs needed:\n" + "\n".join(lines)

    sections = [s for s in [events_text, learnings_text, rsvp_text] if s]
    context = "\n\n".join(sections)

    prompt = f"""\
Format the following weekly summary into a friendly WhatsApp message for a busy parent.
Use short lines and minimal emojis. Include all sections. Keep it scannable.
For learnings, phrase them as "I noticed..." so the parent can correct if wrong.
Do not add any events or information not in the data below.

{context}"""

    summary = await generate(
        prompt=prompt,
        system="You are Radar, a friendly family activity coordinator. "
        "Format weekly summary messages for WhatsApp. Keep messages concise and organized. "
        "Use minimal emojis. Do not invent information.",
    )

    # Mark learnings as surfaced
    if unsurfaced:
        learning_ids = [le.id for le in unsurfaced]
        await learning_dal.mark_surfaced(session, family_id, learning_ids)
        logger.info(
            "Marked %d learnings as surfaced for family %s",
            len(learning_ids),
            family_id,
        )

    logger.info("Generated weekly summary for family %s", family_id)
    return summary.strip()


async def _graduate_confirmed_learnings(
    session: AsyncSession, family_id: UUID, learning_ids: list[UUID]
) -> None:
    """Promote newly confirmed learnings to structured tables where applicable.

    Graduation map:
    - child_school → update children.school
    - child_activity → append to children.activities
    - child_friend → create ChildFriend row
    - contact → update ChildFriend.parent_contact
    """
    from src.state.models import Child, FamilyLearning

    for learning_id in learning_ids:
        learning = await session.get(FamilyLearning, learning_id)
        if not learning or learning.graduated:
            continue

        graduated = False

        if learning.category == "child_school" and learning.entity_id:
            child = await session.get(Child, learning.entity_id)
            if child and child.family_id == family_id:
                child.school = learning.fact
                graduated = True

        elif learning.category == "child_activity" and learning.entity_id:
            child = await session.get(Child, learning.entity_id)
            if child and child.family_id == family_id:
                activities = child.activities or []
                if learning.fact not in activities:
                    child.activities = [*activities, learning.fact]
                graduated = True

        if graduated:
            await learning_dal.graduate_learning(session, learning_id, family_id)
            logger.info(
                "Graduated learning %s (category=%s) for family %s",
                learning_id,
                learning.category,
                family_id,
            )


# ── Immediate Triggers ────────────────────────────────────────────────


async def check_immediate_triggers(
    session: AsyncSession, family_id: UUID
) -> list[str]:
    """Check for conditions that require immediate notification.

    Triggers:
    - RSVP deadline < 48h away and still pending
    - Unclaimed transport for events within 48h

    Returns a list of notification messages (empty if nothing triggered).
    """
    now = datetime.now(UTC)
    cutoff = now + timedelta(hours=48)
    messages: list[str] = []

    # RSVP deadlines within 48h
    rsvp_events = await event_dal.get_events_needing_rsvp(session, family_id)
    for event in rsvp_events:
        if event.rsvp_deadline and event.rsvp_deadline <= cutoff:
            deadline_str = event.rsvp_deadline.strftime("%A %b %d at %I:%M %p")
            messages.append(
                f"RSVP needed for \"{event.title}\" by {deadline_str}. "
                f"Reply to let me know if you'd like to accept or decline."
            )

    # Unclaimed transport within 48h
    upcoming = await event_dal.get_events_in_range(session, family_id, now, cutoff)
    for event in upcoming:
        needs = []
        if event.drop_off_by is None:
            needs.append("drop-off")
        if event.pick_up_by is None:
            needs.append("pick-up")
        if needs:
            time_str = event.datetime_start.strftime("%A at %I:%M %p")
            messages.append(
                f"\"{event.title}\" on {time_str} still needs {' and '.join(needs)} assigned. "
                f"Who's handling it?"
            )

    if messages:
        logger.info(
            "Found %d immediate triggers for family %s",
            len(messages),
            family_id,
        )

    return messages
