"""Reminder Engine — daily digests, weekly summaries, and immediate triggers.

Generates contextual, family-specific notifications using Sonnet for formatting.
"""

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from src.agents.calendar import build_caregiver_name_map
from src.llm import generate
from src.state import events as event_dal
from src.state import families as families_dal
from src.state import learning as learning_dal
from src.state import todos as todos_dal

logger = logging.getLogger(__name__)


# ── Daily Digest ──────────────────────────────────────────────────────


async def generate_daily_digest(
    session: AsyncSession, family_id: UUID
) -> str | None:
    """Generate a daily digest for a family.

    Includes:
    - Today's events
    - Todos due soon (within 48h)
    - Unclaimed transport (events with no drop_off_by or pick_up_by assigned)

    Returns None if nothing actionable (skip sending).
    Uses Sonnet to format into a friendly WhatsApp message.
    """
    # Use family timezone for "today" boundaries
    family = await families_dal.get_family(session, family_id)
    family_tz = family.timezone if family else "America/New_York"

    from src.utils.timezone import get_family_now
    now = get_family_now(family_tz)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start + timedelta(days=1)

    # Today's events (in family timezone)
    todays_events = await event_dal.get_events_in_range(
        session, family_id, today_start, today_end
    )

    # Todos due soon (within 48h)
    upcoming_todos = await todos_dal.get_todos_due_soon(
        session, family_id, within_hours=48, family_timezone=family_tz
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
    if not todays_events and not upcoming_todos and not unclaimed_transport:
        logger.info("No actionable items for daily digest (family %s)", family_id)
        return None

    # Build context for LLM
    # Build event-linked todo lookup
    event_todo_map: dict[UUID, list] = {}
    standalone_todos = []
    for t in upcoming_todos:
        if t.event_id:
            event_todo_map.setdefault(t.event_id, []).append(t)
        else:
            standalone_todos.append(t)

    events_text = ""
    if todays_events:
        lines = []
        for e in todays_events:
            time_str = e.datetime_start.strftime("%I:%M %p") if e.datetime_start else "TBD"
            end_str = f" - {e.datetime_end.strftime('%I:%M %p')}" if e.datetime_end else ""
            location_str = f" at {e.location}" if e.location else ""
            lines.append(f"- {time_str}{end_str}: {e.title}{location_str}")
            # Show linked todos under their event
            for t in event_todo_map.pop(e.id, []):
                due_str = t.due_date.strftime("%a %I:%M %p") if t.due_date else "soon"
                overdue_tag = "OVERDUE — " if t.due_date and t.due_date <= now else ""
                lines.append(f"  → {overdue_tag}{t.description} (due {due_str})")
        events_text = "Today's events:\n" + "\n".join(lines)

    # Remaining event-linked todos (event not in today's list) go to standalone
    for todos in event_todo_map.values():
        standalone_todos.extend(todos)

    todos_text = ""
    if standalone_todos:
        overdue = [t for t in standalone_todos if t.due_date and t.due_date <= now]
        upcoming = [t for t in standalone_todos if t.due_date and t.due_date > now]
        lines = []
        for t in overdue:
            lines.append(f"- OVERDUE: {t.description} (was due {t.due_date.strftime('%a %I:%M %p')})")
        for t in upcoming:
            due_str = t.due_date.strftime("%a %I:%M %p") if t.due_date else "soon"
            lines.append(f"- {t.description} (due {due_str})")
        todos_text = "Todos:\n" + "\n".join(lines)

    transport_text = ""
    if unclaimed_transport:
        # Build caregiver name lookup for per-role status
        caregivers = await families_dal.get_caregivers_for_family(session, family_id)
        cg_map = build_caregiver_name_map(caregivers)

        lines = []
        for e in unclaimed_transport:
            time_str = e.datetime_start.strftime("%I:%M %p") if e.datetime_start else "TBD"
            drop_off_str = cg_map.get(e.drop_off_by, "unassigned") if e.drop_off_by else "unassigned"
            pick_up_str = cg_map.get(e.pick_up_by, "unassigned") if e.pick_up_by else "unassigned"
            lines.append(
                f"- {e.title} at {time_str} — drop-off: {drop_off_str}, pick-up: {pick_up_str}"
            )
        transport_text = "Transport status:\n" + "\n".join(lines)

    sections = [s for s in [events_text, todos_text, transport_text] if s]
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
    # Use family timezone for week boundaries
    family = await families_dal.get_family(session, family_id)
    family_tz = family.timezone if family else "America/New_York"

    from src.utils.timezone import get_family_now
    now = get_family_now(family_tz)
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

    # Confirm previously surfaced learnings (no correction = confirmed)
    # and apply any transport routine confirmations
    try:
        previously_surfaced = await learning_dal.get_learnings_by_category(
            session, family_id, "transport_routine"
        )
        to_confirm = [
            le.id for le in previously_surfaced
            if le.surfaced_in_summary and not le.confirmed
        ]
        if to_confirm:
            await learning_dal.confirm_learnings(session, family_id, to_confirm)
            logger.info(
                "Confirmed %d transport routines for family %s",
                len(to_confirm),
                family_id,
            )

            # Write confirmed routines to RecurringSchedule defaults
            from src.agents.calendar import apply_confirmed_transport_routines

            await apply_confirmed_transport_routines(session, family_id)
    except Exception:
        logger.debug("Could not process transport routine confirmations", exc_info=True)

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


# ── Todo Deadline Nudges ──────────────────────────────────────────────


async def send_todo_deadline_nudges(
    session: AsyncSession, family_id: UUID
) -> int:
    """Send standalone deadline nudges for todos within their reminder window.

    Returns the number of nudges sent.
    """
    family = await families_dal.get_family(session, family_id)
    family_tz = family.timezone if family else None

    todos_to_nudge = await todos_dal.get_todos_needing_reminder(
        session, family_id, family_timezone=family_tz
    )

    if not todos_to_nudge:
        return 0

    from src.actions.whatsapp import send_to_family

    for todo in todos_to_nudge:
        due_str = todo.due_date.strftime("%a %b %d") if todo.due_date else "soon"
        # Reference parent event if linked
        if todo.event_id:
            event = await event_dal.get_event(session, family_id, todo.event_id)
            if event:
                message = f"Reminder: *{todo.description}* for *{event.title}* is due {due_str}."
            else:
                message = f"Reminder: *{todo.description}* is due {due_str}."
        else:
            message = f"Reminder: *{todo.description}* is due {due_str}."
        try:
            await send_to_family(session, family_id, message)
            await todos_dal.mark_reminder_sent(session, family_id, todo.id)
        except Exception:
            logger.exception("Failed to send todo nudge for todo %s", todo.id)

    logger.info(
        "Sent %d todo deadline nudges for family %s",
        len(todos_to_nudge),
        family_id,
    )
    return len(todos_to_nudge)


# ── Immediate Triggers ────────────────────────────────────────────────


async def check_immediate_triggers(
    session: AsyncSession, family_id: UUID
) -> list[str]:
    """Check for conditions that require immediate notification.

    Triggers:
    - RSVP deadline < 48h away and still pending
    - Unclaimed transport for events within 48h
    - Todo deadlines within their reminder window

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

    # Unclaimed transport within 48h (but more than 4h away — avoid double-notifying)
    urgent_cutoff = now + timedelta(hours=4)
    upcoming = await event_dal.get_events_in_range(session, family_id, now, cutoff)
    for event in upcoming:
        needs = []
        if event.drop_off_by is None:
            needs.append("drop-off")
        if event.pick_up_by is None:
            needs.append("pick-up")
        if needs:
            time_str = event.datetime_start.strftime("%A at %I:%M %p")
            if event.datetime_start <= urgent_cutoff:
                # 4h urgent tier
                messages.append(
                    f"⚠️ \"{event.title}\" at "
                    f"{event.datetime_start.strftime('%I:%M %p')} TODAY still has no "
                    f"{' and '.join(needs)} assigned!"
                )
            else:
                # Standard 48h tier
                messages.append(
                    f"\"{event.title}\" on {time_str} still needs "
                    f"{' and '.join(needs)} assigned. Who's handling it?"
                )

    # Note: Todo deadline nudges are handled separately by send_todo_deadline_nudges()
    # which marks reminders as sent. We don't duplicate them here.

    if messages:
        logger.info(
            "Found %d immediate triggers for family %s",
            len(messages),
            family_id,
        )

    return messages
