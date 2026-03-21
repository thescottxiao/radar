"""Calendar Coordinator Agent — the primary reasoning agent for Phase 1.

Handles scheduling queries, event creation, updates, corrections, and
transport assignment claims. Uses Claude Sonnet for generation and
extraction.
"""

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from src.agents.schemas import (
    Conflict,
    ExtractedAssignment,
    ExtractedCorrection,
    ExtractedEvent,
    ExtractedUpdate,
    ResolvedEvent,
)
from src.agents.context import build_family_context
from src.llm import extract, generate
from src.state import children as children_dal
from src.state import events as events_dal
from src.state import memory as memory_dal
from src.state.models import Event, EventSource

logger = logging.getLogger(__name__)

# ── System prompts ──────────────────────────────────────────────────────

CALENDAR_SYSTEM = """\
You are Radar, a helpful family calendar assistant. You help parents
coordinate their kids' activities. Be concise, warm, and practical.

Family context:
{family_context}

Today is {today}. The family timezone is {timezone}.
"""

EVENT_EXTRACTION_SYSTEM = """\
You are extracting event details from a parent's natural language message.
Interpret dates relative to today ({today}). The family timezone is {timezone}.

Family children: {children_names}

Extract the event details as precisely as possible. If the child is not
specified but only one child matches the activity, infer it.
"""

UPDATE_EXTRACTION_SYSTEM = """\
You are extracting event update details from a parent's message.
Today is {today}. The family timezone is {timezone}.

Recent events that might be the target:
{recent_events}
"""

CORRECTION_EXTRACTION_SYSTEM = """\
You are extracting a correction to a recently discussed event.
Today is {today}. The family timezone is {timezone}.

Recently mentioned events:
{recent_events}
"""

ASSIGNMENT_EXTRACTION_SYSTEM = """\
You are extracting a transport assignment claim from a parent's message.
The parent is volunteering to handle drop-off/pick-up for a child.

Family children: {children_names}
Upcoming events needing transport:
{upcoming_events}
"""


# ── Helper: build family context string ─────────────────────────────────


async def _build_family_context(session: AsyncSession, family_id: UUID) -> dict:
    """Build context strings for LLM prompts.

    Delegates to the shared context builder which includes learnings and preferences.
    """
    return await build_family_context(session, family_id)



# ── Public API ──────────────────────────────────────────────────────────


async def handle_query(
    session: AsyncSession,
    family_id: UUID,
    message: str,
    context: dict | None = None,
) -> str:
    """Handle a calendar query like 'What's on Saturday?'

    Queries the Event Registry and formats a natural language response
    using Claude Sonnet.
    """
    ctx = await _build_family_context(session, family_id)

    # Fetch upcoming events (wider window for query)
    upcoming = await events_dal.get_upcoming_events(session, family_id, days=14)

    event_details = []
    for ev in upcoming:
        start_str = ev.datetime_start.strftime("%A, %B %d at %I:%M %p")
        detail = f"- {ev.title}: {start_str}"
        if ev.location:
            detail += f" at {ev.location}"
        if ev.datetime_end:
            detail += f" (ends {ev.datetime_end.strftime('%I:%M %p')})"
        event_details.append(detail)

    events_text = "\n".join(event_details) if event_details else "(no upcoming events)"

    system = CALENDAR_SYSTEM.format(
        family_context=ctx["family_context"],
        today=ctx["today"],
        timezone=ctx["timezone"],
    )

    prompt = (
        f"The parent asks: \"{message}\"\n\n"
        f"Here are the upcoming events:\n{events_text}\n\n"
        f"Answer the parent's question about their calendar. Be concise and helpful. "
        f"If they ask about a specific day, only show events for that day. "
        f"Use a friendly, conversational tone."
    )

    response = await generate(prompt=prompt, system=system)
    return response


async def handle_schedule(
    session: AsyncSession,
    family_id: UUID,
    message: str,
    sender_id: UUID,
    context: dict | None = None,
) -> str:
    """Handle an event creation request like 'Add soccer practice Tuesday at 4pm'.

    Extracts event details via LLM, runs conflict detection, creates the
    Event + GCal events (AUTO action). Returns confirmation or conflict alert.
    """
    ctx = await _build_family_context(session, family_id)

    # Step 1: Extract event details from natural language
    system = EVENT_EXTRACTION_SYSTEM.format(
        today=ctx["today"],
        timezone=ctx["timezone"],
        children_names=", ".join(ctx["children_names"]) if ctx["children_names"] else "none",
    )

    extracted = await extract(
        prompt=message,
        system=system,
        schema=ExtractedEvent,
    )

    # Step 2: Resolve extracted event to concrete datetimes
    resolved = await _resolve_extracted_event(session, family_id, extracted, ctx)

    # Step 3: Check for conflicts
    conflicts = await detect_conflicts(session, family_id, resolved)

    # Step 4: Check for duplicates
    duplicate = await events_dal.find_duplicate_event(
        session, family_id, resolved.title, resolved.datetime_start
    )
    if duplicate:
        return (
            f"It looks like \"{resolved.title}\" is already on the calendar for "
            f"{duplicate.datetime_start.strftime('%A, %B %d at %I:%M %p')}. "
            f"Did you mean to update it?"
        )

    # Step 5: Create the event (AUTO action)
    event = await events_dal.create_event(
        session,
        family_id,
        source=EventSource.manual,
        type=extracted.event_type,
        title=resolved.title,
        datetime_start=resolved.datetime_start,
        datetime_end=resolved.datetime_end,
        location=resolved.location,
        description=resolved.description,
        is_recurring=resolved.is_recurring,
        confirmed_by_caregiver=True,
    )

    # Link children
    if resolved.child_ids:
        await events_dal.link_children_to_event(
            session, family_id, event.id, resolved.child_ids
        )

    # Step 6: Create GCal event (best-effort — gcal module may not be ready)
    try:
        from src.actions.gcal import create_gcal_event

        caregivers = ctx["caregivers"]
        for caregiver in caregivers:
            if caregiver.google_refresh_token_encrypted:
                await create_gcal_event(session, caregiver.id, event)
                break  # Create on first connected calendar
    except (ImportError, Exception) as exc:
        logger.warning("Could not create GCal event: %s", exc)

    # Step 7: Build response
    start_str = resolved.datetime_start.strftime("%A, %B %d at %I:%M %p")
    response = f"Got it! I've added \"{resolved.title}\" on {start_str}"
    if resolved.location:
        response += f" at {resolved.location}"
    response += "."

    if resolved.child_ids and ctx["children"]:
        child_names = [
            c.name for c in ctx["children"] if c.id in resolved.child_ids
        ]
        if child_names:
            response += f" (for {', '.join(child_names)})"

    if conflicts:
        response += "\n\n⚠️ Heads up — I noticed some potential conflicts:"
        for conflict in conflicts:
            response += f"\n• {conflict.description}"

    # Store in conversation memory for correction context
    await memory_dal.store_message(
        session,
        family_id,
        f"Created event: {resolved.title} on {start_str} (ID: {event.id})",
        msg_type="short_term",
    )

    return response


async def handle_update(
    session: AsyncSession,
    family_id: UUID,
    message: str,
    context: dict | None = None,
) -> str:
    """Handle an event update like 'Practice moved to 4pm'."""
    ctx = await _build_family_context(session, family_id)

    # Build recent events text for the LLM
    recent_events_text = _format_events_for_prompt(ctx["upcoming"])

    system = UPDATE_EXTRACTION_SYSTEM.format(
        today=ctx["today"],
        timezone=ctx["timezone"],
        recent_events=recent_events_text,
    )

    extracted = await extract(
        prompt=message,
        system=system,
        schema=ExtractedUpdate,
    )

    # Find the target event
    target = await _find_target_event(
        session, family_id, extracted.target_event_hint, ctx["upcoming"]
    )
    if not target:
        return (
            f"I couldn't find an event matching \"{extracted.target_event_hint}\" "
            f"on your calendar. Could you be more specific?"
        )

    # Build update kwargs
    update_kwargs = {}
    if extracted.new_location:
        update_kwargs["location"] = extracted.new_location
    if extracted.new_title:
        update_kwargs["title"] = extracted.new_title

    # Resolve new datetime if provided
    if extracted.new_date_str or extracted.new_time_str:
        resolved_dt = await _resolve_datetime_update(
            extracted, target, ctx["today"], ctx["timezone"]
        )
        if resolved_dt:
            update_kwargs["datetime_start"] = resolved_dt
    if extracted.new_end_time_str:
        # Resolve end time relative to the (possibly updated) start
        pass  # End time resolution would go here

    if not update_kwargs:
        return "I'm not sure what you'd like to change. Could you be more specific?"

    updated = await events_dal.update_event(
        session, family_id, target.id, **update_kwargs
    )

    # Update GCal (best-effort)
    try:
        from src.actions.gcal import update_gcal_event

        caregivers = ctx["caregivers"]
        for caregiver in caregivers:
            if caregiver.google_refresh_token_encrypted:
                await update_gcal_event(session, caregiver.id, updated)
                break
    except (ImportError, Exception) as exc:
        logger.warning("Could not update GCal event: %s", exc)

    # Build response
    changes = []
    if "datetime_start" in update_kwargs:
        changes.append(
            f"time → {update_kwargs['datetime_start'].strftime('%A, %B %d at %I:%M %p')}"
        )
    if "location" in update_kwargs:
        changes.append(f"location → {update_kwargs['location']}")
    if "title" in update_kwargs:
        changes.append(f"title → {update_kwargs['title']}")

    response = f"Updated \"{target.title}\": {', '.join(changes)}."

    await memory_dal.store_message(
        session,
        family_id,
        f"Updated event: {target.title} — {', '.join(changes)} (ID: {target.id})",
        msg_type="short_term",
    )

    return response


async def handle_correction(
    session: AsyncSession,
    family_id: UUID,
    message: str,
    context: dict | None = None,
) -> str:
    """Handle a correction like 'Actually that's next Saturday'."""
    ctx = await _build_family_context(session, family_id)

    # Get recent conversation memory for context on what was just discussed
    recent_memories = await memory_dal.get_recent_messages(session, family_id, limit=5)
    recent_events_text = _format_events_for_prompt(ctx["upcoming"])

    # Add memory context
    memory_lines = [m.content for m in recent_memories]
    if memory_lines:
        recent_events_text += "\n\nRecent conversation:\n" + "\n".join(memory_lines)

    system = CORRECTION_EXTRACTION_SYSTEM.format(
        today=ctx["today"],
        timezone=ctx["timezone"],
        recent_events=recent_events_text,
    )

    extracted = await extract(
        prompt=message,
        system=system,
        schema=ExtractedCorrection,
    )

    # Find the target event
    target = await _find_target_event(
        session, family_id, extracted.target_event_hint, ctx["upcoming"]
    )
    if not target:
        return (
            "I'm not sure which event you're correcting. "
            "Could you tell me the event name?"
        )

    # Build update kwargs from correction
    update_kwargs = {}
    if extracted.corrected_location:
        update_kwargs["location"] = extracted.corrected_location
    if extracted.corrected_title:
        update_kwargs["title"] = extracted.corrected_title
    if extracted.corrected_date_str or extracted.corrected_time_str:
        resolved_dt = await _resolve_datetime_correction(
            extracted, target, ctx["today"], ctx["timezone"]
        )
        if resolved_dt:
            update_kwargs["datetime_start"] = resolved_dt

    if not update_kwargs:
        return "I'm not sure what to correct. Could you be more specific?"

    updated = await events_dal.update_event(
        session, family_id, target.id, **update_kwargs
    )

    changes = []
    if "datetime_start" in update_kwargs:
        changes.append(
            f"date/time → {update_kwargs['datetime_start'].strftime('%A, %B %d at %I:%M %p')}"
        )
    if "location" in update_kwargs:
        changes.append(f"location → {update_kwargs['location']}")
    if "title" in update_kwargs:
        changes.append(f"name → {update_kwargs['title']}")

    response = f"Corrected! \"{updated.title}\" is now {', '.join(changes)}."

    await memory_dal.store_message(
        session,
        family_id,
        f"Corrected event: {updated.title} — {', '.join(changes)} (ID: {target.id})",
        msg_type="short_term",
    )

    return response


async def handle_assignment_claim(
    session: AsyncSession,
    family_id: UUID,
    message: str,
    sender_id: UUID,
) -> str:
    """Handle a transport assignment claim like 'I'll take Jake'."""
    ctx = await _build_family_context(session, family_id)

    # Filter upcoming events that still need transport
    events_needing_transport = [
        ev for ev in ctx["upcoming"]
        if not ev.drop_off_by or not ev.pick_up_by
    ]

    upcoming_text = _format_events_for_prompt(events_needing_transport)

    system = ASSIGNMENT_EXTRACTION_SYSTEM.format(
        children_names=", ".join(ctx["children_names"]) if ctx["children_names"] else "none",
        upcoming_events=upcoming_text,
    )

    extracted = await extract(
        prompt=message,
        system=system,
        schema=ExtractedAssignment,
    )

    # Resolve child
    child = await children_dal.fuzzy_match_child(
        session, family_id, extracted.child_name
    )
    if not child:
        return (
            f"I'm not sure which child you mean by \"{extracted.child_name}\". "
            f"Your children are: {', '.join(ctx['children_names'])}."
        )

    # Find the target event (if hinted) or the next event for that child
    target_event = None
    if extracted.event_hint:
        target_event = await _find_target_event(
            session, family_id, extracted.event_hint, events_needing_transport
        )

    if not target_event:
        # Find next event that involves this child or has no children linked
        for ev in events_needing_transport:
            target_event = ev
            break

    if not target_event:
        return "I couldn't find an upcoming event that needs transport assignment."

    # Apply assignment
    update_kwargs = {}
    sender_name = None
    for c in ctx["caregivers"]:
        if c.id == sender_id:
            sender_name = c.name or c.whatsapp_phone
            break

    if extracted.role in ("drop_off", "both"):
        update_kwargs["drop_off_by"] = sender_id
    if extracted.role in ("pick_up", "both"):
        update_kwargs["pick_up_by"] = sender_id

    if update_kwargs:
        await events_dal.update_event(
            session, family_id, target_event.id, **update_kwargs
        )

    role_text = {
        "drop_off": "drop-off",
        "pick_up": "pick-up",
        "both": "drop-off and pick-up",
    }.get(extracted.role, "transport")

    response = (
        f"Got it! {sender_name or 'You'} will handle {role_text} "
        f"for {child.name} at \"{target_event.title}\" on "
        f"{target_event.datetime_start.strftime('%A, %B %d at %I:%M %p')}."
    )

    return response


async def detect_conflicts(
    session: AsyncSession,
    family_id: UUID,
    new_event: ResolvedEvent,
) -> list[Conflict]:
    """Check for time overlaps and cross-child location conflicts.

    Returns a list of Conflict objects describing any issues found.
    """
    conflicts: list[Conflict] = []

    # Define the window to check: the duration of the new event
    event_start = new_event.datetime_start
    event_end = new_event.datetime_end or (event_start + timedelta(hours=1))

    # Widen the search window slightly to catch edge cases
    search_start = event_start - timedelta(hours=2)
    search_end = event_end + timedelta(hours=2)

    existing_events = await events_dal.get_events_in_range(
        session, family_id, search_start, search_end
    )

    for existing in existing_events:
        existing_end = existing.datetime_end or (
            existing.datetime_start + timedelta(hours=1)
        )

        # Check time overlap
        if event_start < existing_end and event_end > existing.datetime_start:
            # There is a time overlap — determine type
            conflict_type = "time_overlap"

            # Check if the same children are involved (double-booking)
            if new_event.child_ids and existing.children:
                existing_child_ids = {ec.child_id for ec in existing.children}
                overlapping_children = set(new_event.child_ids) & existing_child_ids
                if overlapping_children:
                    conflict_type = "child_double_book"

            # Check location impossibility (different locations at same time)
            if (
                new_event.location
                and existing.location
                and new_event.location.lower() != existing.location.lower()
                and conflict_type == "child_double_book"
            ):
                conflict_type = "location_impossible"

            description = (
                f"\"{existing.title}\" is at "
                f"{existing.datetime_start.strftime('%I:%M %p')}"
            )
            if existing.location:
                description += f" at {existing.location}"
            description += (
                f", which overlaps with \"{new_event.title}\" at "
                f"{event_start.strftime('%I:%M %p')}"
            )

            child_names = []
            if existing.children:
                # We'd need to look up child names — for now leave empty
                pass

            conflicts.append(
                Conflict(
                    existing_event_id=existing.id,
                    existing_event_title=existing.title,
                    existing_event_start=existing.datetime_start,
                    existing_event_end=existing.datetime_end,
                    existing_event_location=existing.location,
                    conflict_type=conflict_type,
                    description=description,
                    child_names=child_names,
                )
            )

    return conflicts


# ── Private helpers ─────────────────────────────────────────────────────


async def _resolve_extracted_event(
    session: AsyncSession,
    family_id: UUID,
    extracted: ExtractedEvent,
    ctx: dict,
) -> ResolvedEvent:
    """Resolve an ExtractedEvent (with relative date strings) to a ResolvedEvent
    with concrete datetimes.

    Uses the LLM to resolve ambiguous date/time references.
    """
    # Use LLM to resolve the date/time strings to ISO format
    resolve_prompt = (
        f"Convert the following date/time to an ISO 8601 datetime string "
        f"(YYYY-MM-DDTHH:MM:SS) in the timezone {ctx['timezone']}.\n\n"
        f"Today is {ctx['today']}.\n"
        f"Date: {extracted.date_str}\n"
        f"Time: {extracted.time_str or 'not specified (assume 12:00 PM)'}\n\n"
        f"Return ONLY the ISO datetime string, nothing else."
    )
    datetime_str = await generate(
        prompt=resolve_prompt,
        system="You convert natural language dates to ISO 8601 format. Return only the datetime string.",
    )
    datetime_str = datetime_str.strip().strip('"').strip("'")

    try:
        dt_start = datetime.fromisoformat(datetime_str)
    except ValueError:
        # Fallback: try to parse just the date part
        logger.warning("Could not parse datetime: %s, using fallback", datetime_str)
        dt_start = datetime.now(UTC) + timedelta(days=1)

    # Ensure timezone awareness
    if dt_start.tzinfo is None:
        dt_start = dt_start.replace(tzinfo=UTC)

    # Resolve end time
    dt_end = None
    if extracted.end_time_str:
        try:
            end_resolve_prompt = (
                f"Convert '{extracted.end_time_str}' on the same day as "
                f"{dt_start.strftime('%Y-%m-%d')} to ISO 8601 in {ctx['timezone']}.\n"
                f"Return ONLY the ISO datetime string."
            )
            end_str = await generate(
                prompt=end_resolve_prompt,
                system="You convert natural language times to ISO 8601. Return only the datetime string.",
            )
            dt_end = datetime.fromisoformat(end_str.strip().strip('"').strip("'"))
            if dt_end.tzinfo is None:
                dt_end = dt_end.replace(tzinfo=UTC)
        except (ValueError, Exception):
            logger.warning("Could not parse end time: %s", extracted.end_time_str)

    # Resolve child IDs from names
    child_ids = []
    for name in extracted.child_names:
        child = await children_dal.fuzzy_match_child(session, family_id, name)
        if child:
            child_ids.append(child.id)

    return ResolvedEvent(
        title=extracted.title,
        event_type=extracted.event_type,
        datetime_start=dt_start,
        datetime_end=dt_end,
        location=extracted.location,
        child_ids=child_ids,
        description=extracted.description,
        is_recurring=extracted.is_recurring,
        recurrence_pattern=extracted.recurrence_pattern,
    )


async def _resolve_datetime_update(
    extracted: ExtractedUpdate,
    target: Event,
    today: str,
    tz: str,
) -> datetime | None:
    """Resolve date/time from an update extraction."""
    date_part = extracted.new_date_str or target.datetime_start.strftime("%Y-%m-%d")
    time_part = extracted.new_time_str or target.datetime_start.strftime("%H:%M")

    resolve_prompt = (
        f"Convert the following to ISO 8601 datetime (YYYY-MM-DDTHH:MM:SS) "
        f"in timezone {tz}.\nToday is {today}.\n"
        f"Date: {date_part}\nTime: {time_part}\n"
        f"Return ONLY the ISO datetime string."
    )
    result = await generate(
        prompt=resolve_prompt,
        system="You convert natural language dates to ISO 8601. Return only the datetime string.",
    )
    result = result.strip().strip('"').strip("'")
    try:
        dt = datetime.fromisoformat(result)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except ValueError:
        logger.warning("Could not parse resolved datetime: %s", result)
        return None


async def _resolve_datetime_correction(
    extracted: ExtractedCorrection,
    target: Event,
    today: str,
    tz: str,
) -> datetime | None:
    """Resolve date/time from a correction extraction."""
    date_part = extracted.corrected_date_str or target.datetime_start.strftime("%Y-%m-%d")
    time_part = extracted.corrected_time_str or target.datetime_start.strftime("%H:%M")

    resolve_prompt = (
        f"Convert the following to ISO 8601 datetime (YYYY-MM-DDTHH:MM:SS) "
        f"in timezone {tz}.\nToday is {today}.\n"
        f"Date: {date_part}\nTime: {time_part}\n"
        f"Return ONLY the ISO datetime string."
    )
    result = await generate(
        prompt=resolve_prompt,
        system="You convert natural language dates to ISO 8601. Return only the datetime string.",
    )
    result = result.strip().strip('"').strip("'")
    try:
        dt = datetime.fromisoformat(result)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except ValueError:
        logger.warning("Could not parse corrected datetime: %s", result)
        return None


def _format_events_for_prompt(events: list[Event]) -> str:
    """Format a list of events for inclusion in LLM prompts."""
    if not events:
        return "(no upcoming events)"

    lines = []
    for ev in events:
        start_str = ev.datetime_start.strftime("%A, %B %d at %I:%M %p")
        line = f"- \"{ev.title}\" on {start_str}"
        if ev.location:
            line += f" at {ev.location}"
        lines.append(line)
    return "\n".join(lines)


async def _find_target_event(
    session: AsyncSession,
    family_id: UUID,
    hint: str,
    candidates: list[Event],
) -> Event | None:
    """Find an event matching a text hint from a list of candidates.

    Uses token overlap similarity (same as dedup logic).
    """
    from src.state.events import compute_title_similarity

    best_match = None
    best_score = 0.0

    for ev in candidates:
        score = compute_title_similarity(hint, ev.title)
        if score > best_score:
            best_score = score
            best_match = ev

    # Require at least some similarity
    if best_score >= 0.3:
        return best_match

    return None
