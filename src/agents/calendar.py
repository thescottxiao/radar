"""Calendar Coordinator Agent — the primary reasoning agent for Phase 1.

Handles scheduling queries, event creation, updates, corrections, and
transport assignment claims. Uses Claude Sonnet for generation and
extraction.
"""

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from src.agents.schemas import (
    Conflict,
    ExtractedAssignment,
    ExtractedBulkAssignment,
    ExtractedBulkRelease,
    ExtractedCorrection,
    ExtractedEvent,
    ExtractedRelease,
    ExtractedUpdate,
    ResolvedEvent,
)
from src.agents.context import build_family_context
from src.llm import extract, generate
from src.state import children as children_dal
from src.state import events as events_dal
from src.state import families as families_dal
from src.state.models import GcalOutboxOperation
from src.state import learning as learning_dal
from src.state import memory as memory_dal
from src.state import schedules as schedules_dal
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
You are extracting transport assignment(s) from a parent's message.
The parent may be volunteering themselves OR assigning another caregiver.

Family children: {children_names}
Family caregivers: {caregiver_names}
Upcoming events needing transport:
{upcoming_events}
{recent_context}
IMPORTANT:
- Determine the SCOPE of the assignment:
  - If the user says "all of them", "handle everything", "I'll do transport for all",
    "I'll take care of all the events", etc., set scope to "all".
  - If the user names specific events or days (e.g., "Tuesday and Thursday", "soccer and
    the birthday party"), set scope to "specific" and populate the assignments list.
  - Otherwise, set scope to "single" and populate one entry in assignments.
- For scope="all", leave the assignments list empty — all events needing transport will
  be assigned automatically.
- For scope="specific" or "single", populate the assignments list with child_name and
  event_hint for each event mentioned.
- If the recent conversation discusses a specific event (e.g., user asked about "Wed morning
  soccer" and the bot replied about "Soccer practice on Wednesday"), you MUST set event_hint
  to that event name (e.g., "soccer practice"). Do this even if the current message just says
  "handle it" or "Nick will do it" without naming the event.
- If the message names a caregiver (e.g., "Nick has dropoff", "Dad is doing pickup"),
  set assigned_caregiver to that name. If the sender says "I'll handle it" or doesn't
  name anyone, leave assigned_caregiver null.
- A name that matches a caregiver (not a child) should be treated as caregiver assignment.
"""

RELEASE_EXTRACTION_SYSTEM = """\
You are extracting transport release(s) from a parent's message.
The parent is saying they can't cover drop-off or pick-up they were assigned to.

Family children: {children_names}
Upcoming events with transport assigned:
{upcoming_events}

IMPORTANT:
- Determine the SCOPE of the release:
  - If the user says "all of them", "I can't do any", "release everything",
    "I can't do any this week", etc., set scope to "all".
  - If the user names specific events or days (e.g., "Tuesday and Thursday"),
    set scope to "specific" and populate the releases list.
  - Otherwise, set scope to "single" and populate one entry in releases.
- For scope="all", leave the releases list empty — all assigned events are released.
- For scope="specific" or "single", populate the releases list with child_name and
  event_hint for each event mentioned.
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

    # Enqueue GCal update via outbox (async, with retry)
    try:
        from uuid import uuid4
        from src.state import outbox as outbox_dal

        await outbox_dal.enqueue_gcal_write(
            session, family_id, updated.id, GcalOutboxOperation.update, {},
            idempotency_key=f"update:{updated.id}:{uuid4().hex[:12]}",
        )
    except Exception as exc:
        logger.warning("Could not enqueue GCal update: %s", exc)

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


async def _apply_single_assignment(
    session: AsyncSession,
    family_id: UUID,
    target_event: Event,
    role: str,
    assignee_id: UUID,
    child_name: str,
    sender_id: UUID,
    caregivers: list,
) -> tuple[str, str]:
    """Apply transport assignment to a single event.

    Returns (confirmation_line, notification_line).
    """
    update_kwargs: dict = {}
    if role in ("drop_off", "both"):
        update_kwargs["drop_off_by"] = assignee_id
    if role in ("pick_up", "both"):
        update_kwargs["pick_up_by"] = assignee_id

    if update_kwargs:
        await events_dal.update_event(
            session, family_id, target_event.id, **update_kwargs
        )
        for k, v in update_kwargs.items():
            setattr(target_event, k, v)

    role_text = _role_label(role)
    time_str = target_event.datetime_start.strftime("%A, %B %d at %I:%M %p")

    confirm = (
        f"• {role_text} for {child_name} at "
        f"\"{target_event.title}\" on {time_str}"
    )
    notify = (
        f"• {child_name} — \"{target_event.title}\" on {time_str}"
    )

    # Track claim for routine inference
    try:
        for role_key in ("drop_off", "pick_up"):
            if role_key in update_kwargs:
                await track_transport_claim(
                    session, family_id, sender_id, target_event, role_key,
                    caregivers=caregivers,
                )
    except Exception:
        logger.debug("Could not track transport claim for routine inference", exc_info=True)

    # Check off transport prep item
    if target_event.drop_off_by and target_event.pick_up_by and target_event.description:
        updated_desc = target_event.description.replace(
            "☐ Arrange drop-off/pick-up", "☑ Arrange drop-off/pick-up"
        )
        if updated_desc != target_event.description:
            target_event.description = updated_desc
            await events_dal.update_event(
                session, family_id, target_event.id, description=updated_desc
            )

    # Enqueue GCal update
    try:
        from src.state import outbox as outbox_dal

        await outbox_dal.enqueue_gcal_write(
            session, family_id, target_event.id, GcalOutboxOperation.update, {},
            idempotency_key=f"update:{target_event.id}:{uuid4().hex[:12]}",
        )
    except Exception as exc:
        logger.debug("Could not enqueue transport GCal sync: %s", exc)

    return confirm, notify


async def _resolve_target_event(
    session: AsyncSession,
    family_id: UUID,
    assignment: ExtractedAssignment,
    events_needing_transport: list[Event],
    all_upcoming: list[Event],
) -> Event | None:
    """Find the target event for a single extracted assignment."""
    child = await children_dal.fuzzy_match_child(
        session, family_id, assignment.child_name
    )

    # Filter to events linked to this child
    child_events = []
    if child:
        child_events = [
            ev for ev in events_needing_transport
            if ev.children and any(ec.child_id == child.id for ec in ev.children)
        ]

    candidates = child_events or events_needing_transport

    # Date hint filtering
    if assignment.date_hint and isinstance(assignment.date_hint, str):
        date_filtered = _filter_events_by_date_hint(candidates, assignment.date_hint)
        if date_filtered:
            candidates = date_filtered
        else:
            all_date_filtered = _filter_events_by_date_hint(all_upcoming, assignment.date_hint)
            if all_date_filtered:
                candidates = all_date_filtered

    # Event hint matching
    target_event = None
    if assignment.event_hint:
        target_event = await _find_target_event(
            session, family_id, assignment.event_hint, candidates,
        )

    if not target_event and candidates:
        target_event = candidates[0]

    return target_event


def _resolve_assignee(
    assigned_caregiver: str | None,
    sender_id: UUID,
    caregivers: list,
) -> UUID:
    """Resolve which caregiver is being assigned."""
    if assigned_caregiver:
        named = assigned_caregiver.lower().strip()
        for cg in caregivers:
            if cg.name and cg.name.lower() == named:
                return cg.id
    return sender_id


def _get_child_name_for_event(
    event: Event,
    child_id_to_name: dict[UUID, str],
    fallback: str = "your child",
) -> str:
    """Get the child name associated with an event via child_id lookup."""
    if event.children:
        for ec in event.children:
            name = child_id_to_name.get(ec.child_id)
            if name:
                return name
    return fallback


async def handle_assignment_claim(
    session: AsyncSession,
    family_id: UUID,
    message: str,
    sender_id: UUID,
) -> tuple[str, list[str]]:
    """Handle a transport assignment claim — supports single or bulk.

    Returns (response_for_claimer, [notifications_for_others]).
    """
    ctx = await _build_family_context(session, family_id)

    # Filter upcoming events that still need transport
    events_needing_transport = [
        ev for ev in ctx["upcoming"]
        if not ev.drop_off_by or not ev.pick_up_by
    ]

    upcoming_text = _format_events_for_prompt(events_needing_transport)

    # Include recent conversation so the LLM knows which event was just discussed
    recent_memories = await memory_dal.get_recent_messages(session, family_id, limit=5)
    memory_lines = [m.content for m in recent_memories]
    recent_context = ""
    if memory_lines:
        recent_context = "Recent conversation:\n" + "\n".join(memory_lines)

    caregiver_names = [c.name or c.whatsapp_phone for c in ctx["caregivers"]]
    system = ASSIGNMENT_EXTRACTION_SYSTEM.format(
        children_names=", ".join(ctx["children_names"]) if ctx["children_names"] else "none",
        caregiver_names=", ".join(caregiver_names) if caregiver_names else "none",
        upcoming_events=upcoming_text,
        recent_context=recent_context,
    )

    extracted = await extract(
        prompt=message,
        system=system,
        schema=ExtractedBulkAssignment,
    )

    # Refresh children relationships that may have expired after LLM + DB calls
    for ev in ctx["upcoming"]:
        try:
            await session.refresh(ev, ["children"])
        except Exception:
            pass

    # Build child_id → name map for resolving child names from EventChild records
    all_children = await children_dal.get_children_for_family(session, family_id)
    child_id_to_name: dict[UUID, str] = {c.id: c.name for c in all_children}
    fallback_child = ctx["children_names"][0] if ctx["children_names"] else "your child"

    assignee_id = _resolve_assignee(
        extracted.assigned_caregiver, sender_id, ctx["caregivers"]
    )
    assignee_name = _caregiver_display_name(ctx["caregivers"], assignee_id)
    role = extracted.role

    # ── Determine target events based on scope ──
    target_events: list[tuple[Event, str]] = []  # (event, child_name)

    if extracted.scope == "all":
        # Assign all events needing transport
        for ev in events_needing_transport:
            child_name = _get_child_name_for_event(ev, child_id_to_name, fallback_child)
            target_events.append((ev, child_name))

    elif extracted.scope == "specific" and extracted.assignments:
        # Assign specific named events
        for assignment in extracted.assignments:
            target = await _resolve_target_event(
                session, family_id, assignment,
                events_needing_transport, ctx["upcoming"],
            )
            if target:
                child_name = _get_child_name_for_event(target, child_id_to_name, fallback_child)
                target_events.append((target, child_name))

    else:
        # Single event (backward-compatible)
        if extracted.assignments:
            assignment = extracted.assignments[0]
        else:
            # Fallback: extract didn't populate assignments — use first event
            if events_needing_transport:
                ev = events_needing_transport[0]
                child_name = _get_child_name_for_event(ev, child_id_to_name, fallback_child)
                target_events.append((ev, child_name))
            assignment = None

        if assignment and not target_events:
            target = await _resolve_target_event(
                session, family_id, assignment,
                events_needing_transport, ctx["upcoming"],
            )
            if target:
                child_name = _get_child_name_for_event(target, child_id_to_name, fallback_child)
                target_events.append((target, child_name))

    if not target_events:
        return ("I couldn't find an upcoming event that needs transport assignment.", [])

    # ── Apply assignments ──
    confirm_lines: list[str] = []
    notify_lines: list[str] = []
    assigned_event_ids: set[UUID] = set()

    for ev, child_name in target_events:
        confirm, notify = await _apply_single_assignment(
            session, family_id, ev, role, assignee_id,
            child_name, sender_id, ctx["caregivers"],
        )
        confirm_lines.append(confirm)
        notify_lines.append(notify)
        assigned_event_ids.add(ev.id)

    # Batch conflict check across all assigned events
    all_conflicts: list = []
    for ev, _child_name in target_events:
        conflicts = await check_all_transport_conflicts(
            session, family_id, ev, caregiver_filter=assignee_id
        )
        all_conflicts.extend(conflicts)

    # ── Build response ──
    if len(confirm_lines) == 1:
        response = f"✓ {assignee_name or 'You'} — {confirm_lines[0].lstrip('• ')}"
    else:
        response = f"✓ {assignee_name or 'You'} — transport assigned for {len(confirm_lines)} events:\n"
        response += "\n".join(confirm_lines)

    # Conflict warnings
    if all_conflicts:
        response += "\n\n⚠️ Heads up:"
        for c in all_conflicts:
            response += f"\n• {c.description}"

    # Build consolidated notification
    role_text = _role_label(role)
    if len(notify_lines) == 1:
        notification = (
            f"{assignee_name or 'A caregiver'} is handling {role_text} "
            f"for {notify_lines[0].lstrip('• ')}."
        )
    else:
        notification = (
            f"{assignee_name or 'A caregiver'} is handling {role_text} "
            f"for {len(notify_lines)} events:\n"
        )
        notification += "\n".join(notify_lines)

    if all_conflicts:
        conflict_text = "\n\n⚠️ Heads up:"
        for c in all_conflicts:
            conflict_text += f"\n• {c.description}"
        notification += conflict_text

    return response, [notification]


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


# ── Transport coordination ─────────────────────────────────────────────


def _role_label(role: str) -> str:
    """Convert internal role key to display label."""
    return {"drop_off": "drop-off", "pick_up": "pick-up", "both": "drop-off and pick-up"}.get(
        role, "transport"
    )


def _caregiver_display_name(caregivers: list, caregiver_id: UUID) -> str | None:
    """Look up a caregiver's display name from a list."""
    for c in caregivers:
        if c.id == caregiver_id:
            return c.name or c.whatsapp_phone
    return None


def build_caregiver_name_map(caregivers: list) -> dict[UUID, str]:
    """Build {caregiver_id: display_name} mapping."""
    return {c.id: (c.name or c.whatsapp_phone) for c in caregivers}


async def check_transport_gating(
    session: AsyncSession, family_id: UUID, event: Event
) -> tuple[str | None, list]:
    """Check whether transport coordination should run for this event.

    Returns (reason, caregivers) — reason is a string if transport should be
    skipped, or None if coordination should proceed. Caregivers list is returned
    to avoid redundant queries by callers.
    """
    children = await children_dal.get_children_for_family(session, family_id)
    if not children:
        return "no_children", []

    # Check if event has linked children
    if not event.children:
        # Eagerly loaded relationship may be empty — that means no child link
        return "no_child_on_event", []

    caregivers = await families_dal.get_caregivers_for_family(session, family_id)
    if len(caregivers) < 2:
        return "single_caregiver", caregivers

    return None, caregivers


async def auto_assign_single_caregiver(
    session: AsyncSession, family_id: UUID, event_id: UUID, caregiver_id: UUID
) -> None:
    """Silently assign both transport roles to the sole caregiver."""
    await events_dal.update_event(
        session, family_id, event_id,
        drop_off_by=caregiver_id,
        pick_up_by=caregiver_id,
    )


async def detect_sibling_transport_conflicts(
    session: AsyncSession,
    family_id: UUID,
    event: Event,
    role: str,
    caregiver_id: UUID,
) -> list[Conflict]:
    """Check if the assigned caregiver has an overlapping transport duty
    for a *different* child at a *different* location for the *same role*.

    Args:
        role: "drop_off" or "pick_up"

    Note: Prefer ``check_all_transport_conflicts`` when checking both roles
    on the same event — it fetches nearby events once instead of twice.
    """
    event_start = event.datetime_start
    window_start = event_start - timedelta(minutes=30)
    window_end = event_start + timedelta(minutes=30)

    nearby_events = await events_dal.get_events_in_range(
        session, family_id, window_start, window_end
    )

    return _check_sibling_conflicts_against(event, role, caregiver_id, nearby_events)


async def check_all_transport_conflicts(
    session: AsyncSession,
    family_id: UUID,
    event: Event,
    caregiver_filter: UUID | None = None,
) -> list[Conflict]:
    """Check both transport roles on an event for sibling conflicts.

    Fetches nearby events once and checks both drop_off and pick_up roles.
    If caregiver_filter is provided, only checks roles assigned to that caregiver.
    """
    # Pre-fetch nearby events once for both role checks
    event_start = event.datetime_start
    window_start = event_start - timedelta(minutes=30)
    window_end = event_start + timedelta(minutes=30)
    nearby_events = await events_dal.get_events_in_range(
        session, family_id, window_start, window_end
    )

    all_conflicts: list[Conflict] = []
    for role_key in ("drop_off", "pick_up"):
        cg_id = getattr(event, f"{role_key}_by")
        if not cg_id:
            continue
        if caregiver_filter and cg_id != caregiver_filter:
            continue
        conflicts = _check_sibling_conflicts_against(
            event, role_key, cg_id, nearby_events
        )
        all_conflicts.extend(conflicts)

    return all_conflicts


def _check_sibling_conflicts_against(
    event: Event,
    role: str,
    caregiver_id: UUID,
    nearby_events: list[Event],
) -> list[Conflict]:
    """Check for sibling transport conflicts against pre-fetched nearby events."""
    conflicts: list[Conflict] = []
    event_child_ids = {ec.child_id for ec in event.children} if event.children else set()

    for other in nearby_events:
        if other.id == event.id:
            continue

        other_child_ids = {ec.child_id for ec in other.children} if other.children else set()
        if not other_child_ids or other_child_ids == event_child_ids:
            continue
        if other_child_ids & event_child_ids:
            continue

        if not event.location or not other.location:
            continue
        if event.location.lower().strip() == other.location.lower().strip():
            continue

        other_caregiver = (
            other.drop_off_by if role == "drop_off" else other.pick_up_by
        )
        if other_caregiver != caregiver_id:
            continue

        role_label = _role_label(role)
        description = (
            f"Same caregiver is assigned to {role_label} at "
            f"\"{event.title}\" ({event.datetime_start.strftime('%I:%M %p')}, {event.location}) "
            f"and \"{other.title}\" ({other.datetime_start.strftime('%I:%M %p')}, "
            f"{other.location}). One of these needs a different driver."
        )

        conflicts.append(
            Conflict(
                existing_event_id=other.id,
                existing_event_title=other.title,
                existing_event_start=other.datetime_start,
                existing_event_end=other.datetime_end,
                existing_event_location=other.location,
                conflict_type="sibling_transport_conflict",
                description=description,
                child_names=[],
            )
        )

    return conflicts


async def populate_transport_defaults(
    session: AsyncSession, family_id: UUID, event: Event
) -> dict:
    """Auto-populate transport assignments for a newly created event.

    Returns a summary dict with keys: action, conflicts.
    """
    result: dict = {"action": "none", "conflicts": []}

    gate, caregivers = await check_transport_gating(session, family_id, event)

    if gate == "no_children" or gate == "no_child_on_event":
        result["action"] = "skipped"
        return result

    if gate == "single_caregiver":
        if caregivers:
            await auto_assign_single_caregiver(
                session, family_id, event.id, caregivers[0].id
            )
            result["action"] = "auto_assigned_single"
        return result

    # 2+ caregivers — check for recurring schedule defaults
    if event.recurring_schedule_id:
        schedule = await schedules_dal.get_recurring_schedule(
            session, family_id, event.recurring_schedule_id
        )
        if schedule:
            update_kwargs = {}
            if schedule.default_drop_off_caregiver and not event.drop_off_by:
                update_kwargs["drop_off_by"] = schedule.default_drop_off_caregiver
            if schedule.default_pick_up_caregiver and not event.pick_up_by:
                update_kwargs["pick_up_by"] = schedule.default_pick_up_caregiver

            if update_kwargs:
                await events_dal.update_event(
                    session, family_id, event.id, **update_kwargs
                )
                # Refresh event fields for conflict check
                for k, v in update_kwargs.items():
                    setattr(event, k, v)
                result["action"] = "auto_populated"

    # Run sibling conflict check for any assigned roles
    result["conflicts"] = await check_all_transport_conflicts(
        session, family_id, event
    )

    return result


def format_transport_status(
    event: Event, caregiver_map: dict[UUID, str]
) -> str | None:
    """Format transport assignment status for an event.

    Returns None if the event has no children or if both roles are assigned
    (nothing to act on). Otherwise returns a string like
    "drop-off: Mom, pick-up: unassigned".
    caregiver_map: {caregiver_id: display_name}
    """
    if not event.children:
        return None

    parts = []
    if event.drop_off_by:
        name = caregiver_map.get(event.drop_off_by, "someone")
        parts.append(f"drop-off: {name}")
    else:
        parts.append("drop-off: unassigned")

    if event.pick_up_by:
        name = caregiver_map.get(event.pick_up_by, "someone")
        parts.append(f"pick-up: {name}")
    else:
        parts.append("pick-up: unassigned")

    # Only show if at least one role is unassigned
    if event.drop_off_by and event.pick_up_by:
        return None

    return ", ".join(parts)


async def _apply_single_release(
    session: AsyncSession,
    family_id: UUID,
    target_event: Event,
    role: str,
    sender_id: UUID,
) -> tuple[str, str] | None:
    """Release transport for a single event.

    Returns (confirmation_line, notification_line) or None if sender not assigned.
    """
    update_kwargs: dict = {}
    released_roles: list[str] = []

    if role in ("drop_off", "both") and target_event.drop_off_by == sender_id:
        update_kwargs["drop_off_by"] = None
        released_roles.append("drop-off")
    if role in ("pick_up", "both") and target_event.pick_up_by == sender_id:
        update_kwargs["pick_up_by"] = None
        released_roles.append("pick-up")

    if not update_kwargs:
        return None

    await events_dal.update_event(
        session, family_id, target_event.id, **update_kwargs
    )

    role_text = " and ".join(released_roles)
    time_str = target_event.datetime_start.strftime("%A, %B %d at %I:%M %p")

    confirm = f"• {role_text} for \"{target_event.title}\" on {time_str}"
    notify = f"• {role_text.capitalize()} for \"{target_event.title}\" on {time_str}"

    return confirm, notify


async def _resolve_release_target(
    session: AsyncSession,
    family_id: UUID,
    release: ExtractedRelease,
    assigned_events: list[Event],
) -> Event | None:
    """Find the target event for a single extracted release."""
    if release.event_hint:
        target = await _find_target_event(
            session, family_id, release.event_hint, assigned_events
        )
        if target:
            return target

    if release.child_name:
        child = await children_dal.fuzzy_match_child(
            session, family_id, release.child_name
        )
        if child:
            for ev in assigned_events:
                child_ids = {ec.child_id for ec in ev.children} if ev.children else set()
                if child.id in child_ids:
                    return ev

    return assigned_events[0] if assigned_events else None


async def handle_transport_release(
    session: AsyncSession,
    family_id: UUID,
    message: str,
    sender_id: UUID,
) -> tuple[str, list[str]]:
    """Handle a transport release — supports single or bulk.

    Returns (confirmation_message, [notification_messages_for_other_caregivers]).
    """
    ctx = await _build_family_context(session, family_id)

    # Filter to events where this caregiver is assigned
    assigned_events = [
        ev for ev in ctx["upcoming"]
        if ev.drop_off_by == sender_id or ev.pick_up_by == sender_id
    ]

    if not assigned_events:
        return (
            "I don't see any upcoming transport assignments for you to release.",
            [],
        )

    upcoming_text = _format_events_for_prompt(assigned_events)

    system = RELEASE_EXTRACTION_SYSTEM.format(
        children_names=", ".join(ctx["children_names"]) if ctx["children_names"] else "none",
        upcoming_events=upcoming_text,
    )

    extracted = await extract(
        prompt=message,
        system=system,
        schema=ExtractedBulkRelease,
    )

    role = extracted.role

    # ── Determine target events based on scope ──
    target_events: list[Event] = []

    if extracted.scope == "all":
        target_events = assigned_events

    elif extracted.scope == "specific" and extracted.releases:
        for release in extracted.releases:
            target = await _resolve_release_target(
                session, family_id, release, assigned_events,
            )
            if target and target not in target_events:
                target_events.append(target)

    else:
        # Single release
        if extracted.releases:
            target = await _resolve_release_target(
                session, family_id, extracted.releases[0], assigned_events,
            )
            if target:
                target_events.append(target)
        elif assigned_events:
            target_events.append(assigned_events[0])

    if not target_events:
        return ("I couldn't find a matching transport assignment to release.", [])

    # ── Apply releases ──
    confirm_lines: list[str] = []
    notify_lines: list[str] = []

    for ev in target_events:
        result = await _apply_single_release(
            session, family_id, ev, role, sender_id,
        )
        if result:
            confirm, notify = result
            confirm_lines.append(confirm)
            notify_lines.append(notify)

    if not confirm_lines:
        return (
            "You don't appear to be assigned to that role for these events.",
            [],
        )

    # ── Build response ──
    if len(confirm_lines) == 1:
        confirmation = f"Got it — {confirm_lines[0].lstrip('• ')} is now unassigned."
    else:
        confirmation = f"Got it — released transport for {len(confirm_lines)} events:\n"
        confirmation += "\n".join(confirm_lines)

    # Build notification
    if len(notify_lines) == 1:
        notification = f"{notify_lines[0].lstrip('• ')} needs someone. Who can cover?"
    else:
        notification = f"Transport needs coverage for {len(notify_lines)} events:\n"
        notification += "\n".join(notify_lines)
        notification += "\nWho can cover?"

    return (confirmation, [notification])


async def track_transport_claim(
    session: AsyncSession,
    family_id: UUID,
    caregiver_id: UUID,
    event: Event,
    role: str,
    *,
    caregivers: list | None = None,
) -> None:
    """Track a transport claim for routine inference.

    After 3 consistent claims by the same caregiver for the same
    (recurring_schedule, day_of_week, role), creates an unconfirmed
    FamilyLearning entry (category: transport_routine).
    """
    if not event.recurring_schedule_id:
        return

    day_of_week = event.datetime_start.strftime("%A")  # e.g. "Tuesday"
    source_key = f"caregiver:{caregiver_id}|day:{day_of_week}|role:{role}"

    # Look for existing counter
    counter = await learning_dal.get_learning_by_source(
        session,
        family_id,
        category="transport_claim_counter",
        entity_id=event.recurring_schedule_id,
        source=source_key,
    )

    if counter:
        # Parse count from fact field
        current_count = int(counter.fact.split(":")[-1]) if ":" in counter.fact else 0
        new_count = current_count + 1
        counter.fact = f"count:{new_count}"
        await session.flush()
    else:
        new_count = 1
        await learning_dal.create_learning(
            session,
            family_id,
            category="transport_claim_counter",
            fact=f"count:{new_count}",
            source=source_key,
            confidence=0.0,
            entity_type="recurring_schedule",
            entity_id=event.recurring_schedule_id,
        )

    # At threshold, create the transport_routine learning
    if new_count == 3:
        # Look up caregiver name and schedule for human-readable fact
        if not caregivers:
            caregivers = await families_dal.get_caregivers_for_family(session, family_id)
        caregiver_name = _caregiver_display_name(caregivers, caregiver_id) or "A caregiver"

        schedule = await schedules_dal.get_recurring_schedule(
            session, family_id, event.recurring_schedule_id
        )
        activity = schedule.activity_name if schedule else event.title

        fact = f"{caregiver_name} handles {day_of_week} {activity} {_role_label(role)}"

        await learning_dal.create_learning(
            session,
            family_id,
            category="transport_routine",
            fact=fact,
            source=f"caregiver:{caregiver_id}|role:{role}",
            confidence=0.8,
            entity_type="recurring_schedule",
            entity_id=event.recurring_schedule_id,
        )
        logger.info(
            "Transport routine detected: %s (family %s)", fact, family_id
        )


async def apply_confirmed_transport_routines(
    session: AsyncSession, family_id: UUID
) -> None:
    """Write confirmed transport routines to RecurringSchedule defaults.

    Called after weekly summary confirms learnings.
    """
    routines = await learning_dal.get_learnings_by_category(
        session, family_id, "transport_routine"
    )

    for routine in routines:
        if not routine.confirmed or not routine.entity_id:
            continue
        if not routine.source:
            continue

        # Parse caregiver_id and role from source
        parts = dict(p.split(":", 1) for p in routine.source.split("|") if ":" in p)
        caregiver_id_str = parts.get("caregiver")
        role = parts.get("role")

        if not caregiver_id_str or not role:
            continue

        try:
            from uuid import UUID as UUIDType
            caregiver_id = UUIDType(caregiver_id_str)
        except ValueError:
            continue

        update_kwargs = {}
        if role == "drop_off":
            update_kwargs["default_drop_off_caregiver"] = caregiver_id
        elif role == "pick_up":
            update_kwargs["default_pick_up_caregiver"] = caregiver_id
        else:
            continue

        try:
            await schedules_dal.update_schedule_defaults(
                session, family_id, routine.entity_id, **update_kwargs
            )
            logger.info(
                "Applied transport routine to schedule %s: %s",
                routine.entity_id,
                routine.fact,
            )
        except ValueError:
            logger.warning(
                "Could not apply routine — schedule %s not found", routine.entity_id
            )


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


def _filter_events_by_date_hint(
    events: list["Event"], date_hint: str
) -> list["Event"]:
    """Filter events whose date matches a natural-language date hint.

    Handles day names (Mon, Monday, Wed, Wednesday), date strings (March 25),
    and relative references (today, tomorrow, this weekend).
    """
    import re

    hint_lower = date_hint.lower().strip()

    # Map day name abbreviations to full names and weekday numbers (Mon=0)
    _DAY_MAP = {
        "mon": 0, "monday": 0,
        "tue": 1, "tues": 1, "tuesday": 1,
        "wed": 2, "wednesday": 2,
        "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
        "fri": 4, "friday": 4,
        "sat": 5, "saturday": 5,
        "sun": 6, "sunday": 6,
    }

    # Try matching by day of week
    for name, weekday_num in _DAY_MAP.items():
        if name in hint_lower:
            matches = [ev for ev in events if ev.datetime_start.weekday() == weekday_num]
            if matches:
                return matches

    # Try matching by month + day number (e.g., "March 25")
    month_day = re.search(r"(march|april|may|june|july|aug|sept?|oct|nov|dec)\w*\s+(\d{1,2})", hint_lower)
    if month_day:
        day_num = int(month_day.group(2))
        matches = [ev for ev in events if ev.datetime_start.day == day_num]
        if matches:
            return matches

    return []


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
