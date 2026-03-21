"""Intent Router: classifies WhatsApp messages and dispatches to handlers."""

import json
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from src.extraction.schemas import IntentResult, IntentType
from src.llm import HAIKU_MODEL, SONNET_MODEL, classify, extract, generate
from src.state import children as children_dal
from src.state import events as events_dal
from src.state import learning as learning_dal
from src.state import memory as memory_dal
from src.state import pending as pending_dal
from src.state import preferences as pref_dal
from src.state.models import PendingActionStatus, PendingActionType

logger = logging.getLogger(__name__)

# System prompt for intent classification
INTENT_SYSTEM_PROMPT = """\
You are the intent classifier for Radar, a WhatsApp-based family activity assistant.
Classify the user's message into exactly one intent category.

Intent categories:
- add_event: User wants to add or mentions a new event (e.g. "Soccer Saturday at 10am", "I have a birthday party tomorrow evening", "Don't forget Jake has a recital next week")
- query_schedule: Asking about upcoming events or schedule (e.g. "What's on this weekend?")
- modify_event: Wants to change an existing calendar event (e.g. "Move soccer to 3pm")
- cancel_event: Wants to cancel an event (e.g. "Cancel the dentist appointment")
- assign_transport: Assigning drop-off or pick-up (e.g. "I'll take Emma to soccer")
- rsvp_response: Responding to an RSVP prompt (e.g. "Yes to the birthday party")
- add_child_info: Providing child info (e.g. "Jake's shoe size is 3")
- approval_response: Responding to a pending action awaiting approval — includes approving, dismissing, providing details, correcting info, or updating prep tasks. extracted_params must include "action": "approve", "dismiss", or "edit_instruction"
- event_update: Updating an event already on the calendar — marking tasks done, adding notes (e.g. "I bought the wedding gift")
- set_preference: Caregiver is stating a preference or rule for how Radar should behave (e.g. "Don't message me before 7am", "Keep messages short", "I handle school stuff", "No activities on Sundays", "Budget for gifts is $30")
- correct_learning: Correcting a fact or preference Radar learned (e.g. "Actually Emma goes to Washington Elementary", "Her birthday is March 28 not 27", "Change the gift budget to $40")
- general_question: General question about the assistant or non-schedule topic
- greeting: Greeting or small talk
- unknown: Cannot determine intent

Rules:
1. If there is a pending action and the message relates to it (approval, details, corrections, prep task updates), classify as approval_response.
2. If no pending action is relevant, use recent conversation context to inform classification.
3. event_update is only for events already on the calendar, not pending ones.
4. If the context states "There are no pending actions awaiting approval", NEVER classify as approval_response.
5. set_preference is for general behavior preferences, NOT for event-specific changes. "Move soccer to 3pm" is modify_event, not set_preference.
6. correct_learning requires the word "actually" or a clear correction pattern ("not X, it's Y"). Simple new information is add_child_info.

Respond with JSON only: {"intent": "...", "confidence": 0.0-1.0, "extracted_params": {...}}
"""

APPROVAL_KEYWORDS = {
    "approve": ["yes", "approve", "send it", "looks good", "go ahead", "ok", "send", "lgtm", "do it", "confirmed"],
    "dismiss": ["no", "cancel", "dismiss", "nevermind", "never mind", "skip", "don't send", "nah"],
}


async def classify_intent(
    session: AsyncSession,
    family_id: UUID,
    message: str,
    sender_id: UUID,
    button_reply_id: str | None = None,
) -> IntentResult:
    """Classify the intent of a WhatsApp message.

    Button replies are routed directly via their encoded ID (no LLM needed).
    Then checks for active pending actions (approval flow takes priority).
    Then uses Claude Haiku for general intent classification.
    """
    # Button replies route directly — no LLM classification needed
    if button_reply_id:
        from src.utils.button_ids import decode_button_id

        decoded = decode_button_id(button_reply_id)
        if decoded:
            action_map = {"yes": "approve", "no": "dismiss"}
            return IntentResult(
                intent=IntentType.approval_response,
                confidence=1.0,
                extracted_params={"action": action_map.get(decoded["response"], decoded["response"])},
                pending_action_id=UUID(decoded["action_id"]),
            )

    # Check for active pending actions first
    pending_actions = await pending_dal.get_active_pending(session, family_id)

    if pending_actions:
        # Check if message is a response to a pending action
        approval_intent = _check_approval_response(message, pending_actions)
        if approval_intent is not None:
            return approval_intent

    # Build context for classification
    recent_messages = await memory_dal.get_recent_messages(session, family_id, limit=10)
    context_parts = []
    if pending_actions:
        most_recent = pending_actions[0]
        context_parts.append(
            f"There is a pending action (type: {most_recent.type.value}) awaiting approval.\n"
            f"Pending action content:\n{most_recent.draft_content}"
        )
    else:
        context_parts.append("There are no pending actions awaiting approval.")
    if recent_messages:
        recent_texts = [m.content for m in reversed(recent_messages[-10:])]
        context_parts.append("Recent conversation:\n" + "\n".join(recent_texts))

    context = "\n\n".join(context_parts) if context_parts else "No recent context."
    prompt = f"Context:\n{context}\n\nUser message: {message}"

    try:
        raw = await classify(prompt, INTENT_SYSTEM_PROMPT, model=HAIKU_MODEL)
        parsed = _parse_classification_response(raw)

        # If the LLM classified as approval_response, attach the pending action ID
        if parsed.intent == IntentType.approval_response and pending_actions:
            parsed.pending_action_id = pending_actions[0].id
        elif parsed.intent == IntentType.approval_response and not pending_actions:
            # No active pending actions — LLM was confused by conversation history.
            logger.warning(
                "LLM classified as approval_response but no pending actions exist. "
                "Falling back to add_event for message: %s", message[:100]
            )
            parsed.intent = IntentType.add_event
            parsed.confidence = parsed.confidence * 0.5

        return parsed
    except Exception:
        logger.exception("Intent classification failed for message: %s", message[:100])
        return IntentResult(
            intent=IntentType.unknown,
            confidence=0.0,
            extracted_params={"raw_message": message},
        )


def _check_approval_response(
    message: str, pending_actions: list
) -> IntentResult | None:
    """Check if message is a response to a pending action.

    Returns an IntentResult if it matches, None otherwise.
    """
    if not pending_actions:
        return None

    msg_lower = message.strip().lower()

    # Check for approval keywords
    for keyword in APPROVAL_KEYWORDS["approve"]:
        if msg_lower == keyword or msg_lower.startswith(keyword + " "):
            return IntentResult(
                intent=IntentType.approval_response,
                confidence=0.9,
                extracted_params={"action": "approve"},
                pending_action_id=pending_actions[0].id,
            )

    # Check for dismissal keywords
    for keyword in APPROVAL_KEYWORDS["dismiss"]:
        if msg_lower == keyword or msg_lower.startswith(keyword + " "):
            return IntentResult(
                intent=IntentType.approval_response,
                confidence=0.9,
                extracted_params={"action": "dismiss"},
                pending_action_id=pending_actions[0].id,
            )

    # If the message seems like an edit instruction for the pending action
    # (e.g., "change the time to 3pm", "make it more formal")
    edit_prefixes = ["change", "edit", "update", "make it", "rewrite", "modify", "adjust"]
    for prefix in edit_prefixes:
        if msg_lower.startswith(prefix):
            return IntentResult(
                intent=IntentType.approval_response,
                confidence=0.8,
                extracted_params={
                    "action": "edit_instruction",
                    "instruction": message,
                },
                pending_action_id=pending_actions[0].id,
            )

    # For other messages (e.g., "It's 7pm at Ryan's house"), fall through
    # to the LLM classifier which has pending action context and can decide
    # whether the message is an edit, a new intent, or something unrelated.
    return None


def _parse_classification_response(raw: str) -> IntentResult:
    """Parse the LLM classification response into an IntentResult."""
    # Strip markdown code fences if present
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first and last lines (fences)
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        text = text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Could not parse classification response: %s", text[:200])
        return IntentResult(
            intent=IntentType.unknown,
            confidence=0.0,
            extracted_params={"raw_response": text},
        )

    intent_str = data.get("intent", "unknown")
    try:
        intent = IntentType(intent_str)
    except ValueError:
        intent = IntentType.unknown

    return IntentResult(
        intent=intent,
        confidence=float(data.get("confidence", 0.5)),
        extracted_params=data.get("extracted_params", {}),
    )


async def route_intent(
    session: AsyncSession,
    family_id: UUID,
    intent: IntentResult,
    message: str,
    sender_id: UUID,
) -> str:
    """Dispatch a classified intent to the appropriate handler.

    Returns the response text to send back via WhatsApp.
    """
    handlers = {
        IntentType.add_event: _handle_add_event,
        IntentType.query_schedule: _handle_query_schedule,
        IntentType.modify_event: _handle_modify_event,
        IntentType.cancel_event: _handle_cancel_event,
        IntentType.assign_transport: _handle_assign_transport,
        IntentType.rsvp_response: _handle_rsvp_response,
        IntentType.add_child_info: _handle_add_child_info,
        IntentType.approval_response: _handle_approval_response,
        IntentType.event_update: _handle_event_update,
        IntentType.set_preference: _handle_set_preference,
        IntentType.correct_learning: _handle_correct_learning,
        IntentType.general_question: _handle_general_question,
        IntentType.greeting: _handle_greeting,
        IntentType.unknown: _handle_unknown,
    }

    handler = handlers.get(intent.intent, _handle_unknown)

    try:
        return await handler(session, family_id, intent, message, sender_id)
    except Exception:
        logger.exception(
            "Handler for %s failed (family=%s)", intent.intent, family_id
        )
        return "Sorry, something went wrong processing your message. Please try again."


# ── Shared helpers ─────────────────────────────────────────────────────


async def _get_local_now(session: AsyncSession, family_id: UUID) -> datetime:
    """Get the current datetime in the family's local timezone."""
    from zoneinfo import ZoneInfo

    from src.state import families as families_dal

    family = await families_dal.get_family(session, family_id)
    family_tz = ZoneInfo(family.timezone) if family else ZoneInfo("America/New_York")
    return datetime.now(family_tz)


async def _gather_event_context(
    session: AsyncSession,
    family_id: UUID,
    message: str,
    default_days: int = 90,
) -> tuple[str, str]:
    """Gather two-tier event context for smart event matching.

    Tier 1: Recent conversation messages for context.
    Tier 2: Calendar events from GCal (source of truth), with local DB fallback.

    Returns (conversation_context, calendar_context) as formatted strings.
    """
    # Tier 1: Conversation context
    recent_messages = await memory_dal.get_recent_messages(session, family_id, limit=10)
    conversation_context = ""
    if recent_messages:
        recent_texts = [m.content for m in reversed(recent_messages[-10:])]
        conversation_context = "\n".join(recent_texts)

    # Tier 2: Calendar events from GCal
    days = default_days
    gcal_context = ""
    gcal_events: list[dict] = []
    try:
        from src.actions.gcal import list_upcoming_events

        gcal_events = await list_upcoming_events(session, family_id, days=days)
        if gcal_events:
            gcal_context = _format_gcal_events(gcal_events)
    except Exception:
        logger.warning("Could not fetch GCal events for context (family %s)", family_id)

    # Fallback to local DB
    if not gcal_events:
        try:
            local_events = await events_dal.get_upcoming_events(session, family_id, days=days)
            if local_events:
                gcal_context = _format_local_events(local_events)
        except Exception:
            logger.warning("Could not fetch local events for context (family %s)", family_id)

    return conversation_context, gcal_context


def _format_gcal_events(events: list[dict]) -> str:
    """Format GCal events into a string for LLM context."""
    summaries = []
    for ev in events:
        summary = f"- {ev['title']} ({ev.get('start', 'TBD')}) [gcal_id: {ev.get('gcal_id', 'unknown')}]"
        if ev.get("description"):
            summary += f"\n  Description: {ev['description'][:500]}"
        if ev.get("location"):
            summary += f"\n  Location: {ev['location']}"
        summaries.append(summary)
    return "\n".join(summaries)


def _format_local_events(events: list) -> str:
    """Format local DB events into a string for LLM context."""
    summaries = []
    for ev in events:
        dt_str = ev.datetime_start.strftime("%a %b %d, %I:%M %p") if ev.datetime_start else "TBD"
        summary = f"- {ev.title} ({dt_str})"
        if ev.description:
            summary += f"\n  Description: {ev.description[:500]}"
        if ev.location:
            summary += f"\n  Location: {ev.location}"
        summaries.append(summary)
    return "\n".join(summaries)


# ── Intent handlers ────────────────────────────────────────────────────
# Phase 1 implementations — basic versions that will be expanded in later phases.


async def _handle_add_event(
    session: AsyncSession,
    family_id: UUID,
    intent: IntentResult,
    message: str,
    sender_id: UUID,
) -> str:
    """Handle add_event intent: extract details, create event directly.

    Flow: extract → check missing details → if complete, create event in DB + GCal.
    If details are missing, create a pending action to collect them.
    Manual events are auto-added (no confirmation step).
    """
    from src.extraction.schemas import ExtractedEvent
    from src.llm import extract, generate

    # Use family's timezone for accurate "today" context
    local_now = await _get_local_now(session, family_id)

    system = (
        "Extract event details from the user's message. "
        "Include title, date/time, location, and which child it's for if mentioned. "
        f"The user's timezone is {local_now.tzinfo}. "
        "Use the current date context: today is "
        + local_now.strftime("%A, %B %d, %Y") + ", "
        + local_now.strftime("%I:%M %p") + " local time. "
        "If the user says 'tomorrow evening' without a specific time, infer a reasonable "
        "time (e.g. 6:00 PM for evening, 12:00 PM for noon/lunch, 9:00 AM for morning). "
        "All datetimes should include timezone info."
    )

    try:
        extracted = await extract(message, system, ExtractedEvent)
    except Exception:
        logger.exception("Event extraction failed")
        return (
            "I understood you want to add an event but couldn't extract the details. "
            "Could you try again with the date, time, and title?"
        )

    if not extracted.datetime_start:
        return (
            f"Got it — \"{extracted.title}\". "
            "When is it? Please include the date and time."
        )

    # Check if key details are missing — ask before creating pending action
    missing = []
    if not extracted.time_explicit:
        missing.append("what time")
    if not extracted.location:
        missing.append("where")

    if missing:
        dt_str = extracted.datetime_start.strftime("%A, %B %d") if extracted.datetime_start else "soon"
        missing_q = " and ".join(missing)
        ask_text = (
            f"Got it — *{extracted.title}* on {dt_str}. "
            f"{missing_q.capitalize()} is it?"
        )

        # Serialize partial event data for storage
        event_data = extracted.model_dump(mode="json")

        # Dismiss any existing detail-collection pending actions
        active_pending = await pending_dal.get_active_pending(session, family_id)
        for pa in active_pending:
            if pa.context.get("missing_fields"):
                await pending_dal.resolve_pending(
                    session, family_id, pa.id,
                    status=PendingActionStatus.dismissed, resolved_by=sender_id,
                )

        # Create pending action with partial data — expires in 1 hour
        await pending_dal.create_pending_action(
            session,
            family_id=family_id,
            action_type=PendingActionType.event_confirmation,
            draft_content=ask_text,
            context={
                "event_data": event_data,
                "source": "manual",
                "missing_fields": missing,
            },
            initiated_by=sender_id,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

        # Store in conversation memory for classifier context
        await memory_dal.store_message(
            session, family_id=family_id,
            content=f"Radar: {ask_text}",
            msg_type="short_term",
            expires_at=datetime.now(UTC) + timedelta(hours=24),
        )

        return ask_text

    # All details present — create event directly (no confirmation needed for manual events)
    event_kwargs: dict = {
        "title": extracted.title,
        "source": "manual",
        "type": extracted.event_type or "other",
        "datetime_start": extracted.datetime_start,
        "extraction_confidence": extracted.confidence,
        "confirmed_by_caregiver": True,
    }
    if extracted.datetime_end:
        event_kwargs["datetime_end"] = extracted.datetime_end
    if extracted.location:
        event_kwargs["location"] = extracted.location
    if extracted.description:
        event_kwargs["description"] = extracted.description

    event = await events_dal.create_event(session, family_id, **event_kwargs)

    # Write to Google Calendar (source of truth)
    try:
        from src.actions.gcal import create_calendar_event

        gcal_ids = await create_calendar_event(session, family_id, event)
        if gcal_ids:
            logger.info("Created GCal event(s) %s for '%s'", gcal_ids, extracted.title)
    except Exception:
        logger.exception("Failed to create GCal event for '%s' — saved locally only", extracted.title)

    # Build response
    dt_str = extracted.datetime_start.strftime("%a %b %d, %I:%M %p")
    parts = [f"✅ Added to your calendar: *{extracted.title}*", f"{dt_str}"]
    if extracted.location:
        parts.append(f"📍 {extracted.location}")

    # Generate prep tips (shown after event is added)
    try:
        tip_prompt = f"""\
Event: {extracted.title}
Date: {dt_str}
Location: {extracted.location or "TBD"}

List ONLY tasks that are clearly important and specific to this event type. Examples:
- Birthday party → "Buy birthday gift"
- Sports → "Pack gear bag"
- Medical → "Bring insurance card"

Do NOT include generic advice like "confirm details", "check parking", "plan outfit", \
"get directions", or "check weather". If nothing specific is needed, return NOTHING.

Use "•" prefix."""
        tips = await generate(
            tip_prompt,
            system="You are a concise family assistant. Return only genuinely important, event-specific bullet points. If nothing specific is needed, return nothing at all.",
        )
        if tips and tips.strip():
            parts.append("")
            parts.append("*Heads up:*")
            tip_lines = [line.strip() for line in tips.strip().split("\n") if line.strip().startswith("•")]
            parts.extend(tip_lines)
    except Exception:
        logger.debug("Could not generate prep tips for '%s'", extracted.title)

    return "\n".join(parts)


async def _handle_query_schedule(
    session: AsyncSession,
    family_id: UUID,
    intent: IntentResult,
    message: str,
    sender_id: UUID,
) -> str:
    """Handle query_schedule intent: query GCal (source of truth) with local DB fallback."""
    from src.llm import generate

    # Determine query range from params
    days = intent.extracted_params.get("days", 7)
    try:
        days = int(days)
    except (ValueError, TypeError):
        days = 7

    # Try Google Calendar first (source of truth)
    event_lines = []
    source = "gcal"
    try:
        from src.actions.gcal import list_upcoming_events

        gcal_events = await list_upcoming_events(session, family_id, days=days)
        if gcal_events:
            for ev in gcal_events:
                start = ev.get("start", "")
                # Parse ISO datetime or date string for display
                try:
                    from datetime import datetime as dt

                    if "T" in start:
                        dt_obj = dt.fromisoformat(start)
                        dt_str = dt_obj.strftime("%a %b %d, %I:%M %p")
                    else:
                        dt_obj = dt.fromisoformat(start)
                        dt_str = dt_obj.strftime("%a %b %d")
                except (ValueError, TypeError):
                    dt_str = start

                line = f"- {ev['title']} — {dt_str}"
                if ev.get("location"):
                    line += f" @ {ev['location']}"
                event_lines.append(line)
    except Exception:
        logger.warning("GCal query failed for family %s, falling back to local DB", family_id)
        source = "local"

    # Fallback to local DB if GCal returned nothing or failed
    if not event_lines:
        source = "local"
        events = await events_dal.get_upcoming_events(session, family_id, days=days)
        for ev in events:
            dt_str = ev.datetime_start.strftime("%a %b %d, %I:%M %p")
            line = f"- {ev.title} — {dt_str}"
            if ev.location:
                line += f" @ {ev.location}"
            event_lines.append(line)

    if not event_lines:
        return f"Nothing on the calendar for the next {days} days."

    event_list = "\n".join(event_lines)
    logger.info("Schedule query for family %s: %d events from %s", family_id, len(event_lines), source)

    # Use LLM to generate a natural-language summary
    system = (
        "You are Radar, a friendly family calendar assistant. "
        "Summarize the upcoming schedule naturally and concisely. "
        "Use a warm, helpful tone. Keep it brief."
    )
    prompt = f"User asked: {message}\n\nUpcoming events:\n{event_list}"

    try:
        response = await generate(prompt, system)
        return response
    except Exception:
        # Fallback to simple list
        return f"Here's what's coming up:\n{event_list}"


async def _handle_modify_event(
    session: AsyncSession,
    family_id: UUID,
    intent: IntentResult,
    message: str,
    sender_id: UUID,
) -> str:
    """Handle modify_event intent: identify the event and apply changes.

    Two-tier context strategy:
    1. Check recent conversation for context about which event the user means.
    2. Query GCal (source of truth) to fuzzy-match the message against event titles.
    """
    from src.llm import generate

    conversation_context, gcal_context = await _gather_event_context(
        session, family_id, message
    )
    _local_today = (await _get_local_now(session, family_id)).strftime("%A, %B %d, %Y")

    if not conversation_context and not gcal_context:
        return (
            "I can help modify that event. "
            "Could you tell me which event and what you'd like to change?"
        )

    system = """\
You are Radar, a family calendar assistant. The user wants to modify an existing event.

Your job:
1. Resolve any relative date references in the user's message (e.g., "tomorrow", "next week", \
"this Saturday") using today's date. This is critical for matching the correct event.
2. Figure out which event they're referring to by matching the resolved date AND description \
against the calendar events. Date match takes priority over conversation context.
3. Determine what they want to change (time, location, title, description, etc.).
4. Return a JSON response with:
   - "matched_event": the title of the event they're referring to (or null if you can't determine it)
   - "gcal_id": the gcal_id of the matched event if available (or null)
   - "modifications": a dict of fields to update. Valid keys:
     - "summary": new title
     - "start": new start datetime in ISO 8601 format
     - "end": new end datetime in ISO 8601 format (if inferrable)
     - "location": new location
     - "description": new or updated description
   - "confirmation_message": a friendly confirmation message describing the change

Only output the JSON. No other text."""

    prompt = f"""User message: {message}

Today's date: {_local_today}

Recent conversation:
{conversation_context if conversation_context else "(No recent conversation)"}

Upcoming calendar events:
{gcal_context if gcal_context else "(No events found)"}"""

    try:
        raw = await generate(prompt, system)
        text = raw.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:]).strip()

        data = json.loads(text)

        matched_event = data.get("matched_event")
        if not matched_event:
            return (
                "I couldn't figure out which event you mean. "
                "Could you mention the event name or date?"
            )

        gcal_id = data.get("gcal_id")
        modifications = data.get("modifications", {})

        if not modifications:
            return (
                f"I found *{matched_event}*, but I'm not sure what to change. "
                "Could you tell me what you'd like to modify?"
            )

        # Push changes to GCal
        if gcal_id and modifications:
            try:
                gcal_body: dict = {}
                if "summary" in modifications:
                    gcal_body["summary"] = modifications["summary"]
                if "start" in modifications:
                    gcal_body["start"] = {"dateTime": modifications["start"]}
                if "end" in modifications:
                    gcal_body["end"] = {"dateTime": modifications["end"]}
                if "location" in modifications:
                    gcal_body["location"] = modifications["location"]
                if "description" in modifications:
                    gcal_body["description"] = modifications["description"]

                if gcal_body:
                    from src.auth.google_client import get_calendar_service, get_google_credentials
                    caregivers = await families_dal.get_caregivers_for_family(session, family_id)
                    for caregiver in caregivers:
                        if caregiver.google_refresh_token_encrypted is None:
                            continue
                        try:
                            credentials = await get_google_credentials(session, caregiver.id)
                            service = get_calendar_service(credentials)
                            service.events().patch(
                                calendarId="primary",
                                eventId=gcal_id,
                                body=gcal_body,
                            ).execute()
                            logger.info("Updated GCal event %s for caregiver %s", gcal_id, caregiver.id)
                            break
                        except Exception:
                            logger.debug("Could not update GCal event %s for caregiver %s", gcal_id, caregiver.id)
            except Exception:
                logger.exception("Failed to push event modification to GCal")

        # Update local DB event
        try:
            local_events = await events_dal.get_upcoming_events(session, family_id, days=90)
            for ev in local_events:
                if ev.title and matched_event.lower() in ev.title.lower():
                    update_kwargs: dict = {}
                    if "summary" in modifications:
                        update_kwargs["title"] = modifications["summary"]
                    if "start" in modifications:
                        from datetime import datetime as dt
                        update_kwargs["datetime_start"] = dt.fromisoformat(modifications["start"])
                    if "end" in modifications:
                        from datetime import datetime as dt
                        update_kwargs["datetime_end"] = dt.fromisoformat(modifications["end"])
                    if "location" in modifications:
                        update_kwargs["location"] = modifications["location"]
                    if "description" in modifications:
                        update_kwargs["description"] = modifications["description"]
                    if update_kwargs:
                        await events_dal.update_event(session, family_id, ev.id, **update_kwargs)
                        logger.info("Updated local event '%s'", ev.title)
                    break
        except Exception:
            logger.debug("Could not update local event for '%s'", matched_event)

        return data.get("confirmation_message", f"Updated *{matched_event}* ✓")

    except json.JSONDecodeError:
        logger.warning("Could not parse modify LLM response: %s", raw[:200] if raw else "empty")
        return (
            "I understood you want to modify an event, but I had trouble processing it. "
            "Could you try again?"
        )
    except Exception:
        logger.exception("Modify event handler failed")
        return "Sorry, I couldn't process that modification. Please try again."


async def _handle_cancel_event(
    session: AsyncSession,
    family_id: UUID,
    intent: IntentResult,
    message: str,
    sender_id: UUID,
) -> str:
    """Handle cancel_event intent: identify the event and cancel it.

    Two-tier context strategy:
    1. Check recent conversation for context about which event the user means.
    2. Query GCal (source of truth) to fuzzy-match the message against event titles.
    """
    from src.llm import generate

    conversation_context, gcal_context = await _gather_event_context(
        session, family_id, message
    )
    _local_today = (await _get_local_now(session, family_id)).strftime("%A, %B %d, %Y")

    if not conversation_context and not gcal_context:
        return (
            "Which event would you like to cancel? "
            "Please give me the name or date so I can find it."
        )

    system = """\
You are Radar, a family calendar assistant. The user wants to cancel an event.

Your job:
1. Resolve any relative date references in the user's message (e.g., "tomorrow", "next week", \
"this Saturday") using today's date. This is critical for matching the correct event.
2. Figure out which event they're referring to by matching the resolved date AND description \
against the calendar events. Date match takes priority over conversation context.
3. Return a JSON response with:
   - "matched_event": the title of the event they're referring to (or null if you can't determine it)
   - "gcal_id": the gcal_id of the matched event if available (or null)
   - "confirmation_message": a friendly confirmation message (e.g., "Cancelled \\\"Soccer Practice\\\" on Saturday.")

Only output the JSON. No other text."""

    prompt = f"""User message: {message}

Today's date: {_local_today}

Recent conversation:
{conversation_context if conversation_context else "(No recent conversation)"}

Upcoming calendar events:
{gcal_context if gcal_context else "(No events found)"}"""

    try:
        raw = await generate(prompt, system)
        text = raw.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:]).strip()

        data = json.loads(text)

        matched_event = data.get("matched_event")
        if not matched_event:
            return (
                "I couldn't figure out which event you mean. "
                "Could you mention the event name or date?"
            )

        gcal_id = data.get("gcal_id")

        # Cancel from GCal
        if gcal_id:
            try:
                from src.actions.gcal import delete_gcal_event_by_id
                await delete_gcal_event_by_id(session, family_id, gcal_id)
            except Exception:
                logger.exception("Failed to delete GCal event %s", gcal_id)

        # Soft-delete locally
        try:
            local_events = await events_dal.get_upcoming_events(session, family_id, days=90)
            for ev in local_events:
                if ev.title and matched_event.lower() in ev.title.lower():
                    await events_dal.update_event(
                        session, family_id, ev.id,
                        description=(ev.description or "") + "\n[CANCELLED]",
                    )
                    logger.info("Soft-deleted local event '%s'", ev.title)
                    break
        except Exception:
            logger.debug("Could not soft-delete local event for '%s'", matched_event)

        return data.get("confirmation_message", f"Cancelled *{matched_event}* ✓")

    except json.JSONDecodeError:
        logger.warning("Could not parse cancel LLM response: %s", raw[:200] if raw else "empty")
        return (
            "I understood you want to cancel an event, but I had trouble processing it. "
            "Could you try again?"
        )
    except Exception:
        logger.exception("Cancel event handler failed")
        return "Sorry, I couldn't process that cancellation. Please try again."


async def _handle_assign_transport(
    session: AsyncSession,
    family_id: UUID,
    intent: IntentResult,
    message: str,
    sender_id: UUID,
) -> str:
    """Handle assign_transport intent."""
    return "Got it, I'll note that transport assignment. Which event is this for?"


async def _handle_rsvp_response(
    session: AsyncSession,
    family_id: UUID,
    intent: IntentResult,
    message: str,
    sender_id: UUID,
) -> str:
    """Handle rsvp_response intent."""
    return (
        "I'll update the RSVP. "
        "Which event is this for, and are you saying yes or no?"
    )


async def _handle_add_child_info(
    session: AsyncSession,
    family_id: UUID,
    intent: IntentResult,
    message: str,
    sender_id: UUID,
) -> str:
    """Handle add_child_info intent."""
    await learning_dal.create_learning(
        session,
        family_id=family_id,
        category="child_info",
        fact=message,
        source="whatsapp",
    )
    return "Noted! I'll remember that."


async def _handle_approval_response(
    session: AsyncSession,
    family_id: UUID,
    intent: IntentResult,
    message: str,
    sender_id: UUID,
) -> str:
    """Handle approval_response intent for pending actions."""
    action_type = intent.extracted_params.get("action", "")
    pending_action_id = intent.pending_action_id

    if not pending_action_id:
        return "I'm not sure which action you're responding to. Could you clarify?"

    if action_type == "approve":
        # Check if this is an event confirmation — need to create the event
        pending_action = await pending_dal.get_pending_action(
            session, family_id, pending_action_id
        )
        if pending_action and pending_action.type.value == "event_confirmation":
            # If still collecting details, treat as edit — merge the user's
            # details into event_data before creating (don't lose them)
            if pending_action.context.get("missing_fields"):
                return await _handle_event_confirmation_edit(
                    session, family_id, pending_action, message,
                )
            response = await _create_event_from_pending(session, family_id, pending_action)
        else:
            response = "Approved! I'll take care of it."

        await pending_dal.resolve_pending(
            session,
            family_id=family_id,
            action_id=pending_action_id,
            status=PendingActionStatus.approved,
            resolved_by=sender_id,
        )
        return response

    elif action_type == "dismiss":
        await pending_dal.resolve_pending(
            session,
            family_id=family_id,
            action_id=pending_action_id,
            status=PendingActionStatus.dismissed,
            resolved_by=sender_id,
        )
        # Check if this was an event confirmation for a friendlier message
        pending_action = await pending_dal.get_pending_action(
            session, family_id, pending_action_id
        )
        if pending_action and pending_action.type.value == "event_confirmation":
            return "Got it, skipped."
        return "No problem, I've dismissed that."

    elif action_type == "edit_instruction":
        instruction = intent.extracted_params.get("instruction", message)
        from src.llm import generate

        pending_actions = await pending_dal.get_active_pending(session, family_id)
        current_action = next(
            (a for a in pending_actions if a.id == pending_action_id), None
        )
        if not current_action:
            return "I couldn't find that pending action. It may have expired."

        # Event confirmations: update the event data, not just the draft text
        if current_action.type == PendingActionType.event_confirmation:
            return await _handle_event_confirmation_edit(
                session, family_id, current_action, instruction,
            )

        # Generic draft revision for non-event actions
        system = (
            "You are revising a draft message based on the user's instruction. "
            "Return only the revised message text."
        )
        prompt = (
            f"Original draft:\n{current_action.draft_content}\n\n"
            f"User's edit instruction: {instruction}\n\n"
            "Revised draft:"
        )
        try:
            new_draft = await generate(prompt, system)
            await pending_dal.update_draft(
                session,
                family_id=family_id,
                action_id=current_action.id,
                new_draft=new_draft,
                edit_instruction=instruction,
            )
            return f"Updated draft:\n\n{new_draft}\n\nLook good? Reply 'yes' to send or suggest more changes."
        except Exception:
            logger.exception("Failed to revise draft")
            return "Sorry, I couldn't revise the draft. Please try again."

    return "I'm not sure what you'd like to do with that action. You can approve, dismiss, or suggest edits."


async def _create_event_from_pending(
    session: AsyncSession, family_id: UUID, pending_action
) -> str:
    """Create an event from a pending event_confirmation action's context."""
    from datetime import datetime as dt

    event_data = pending_action.context.get("event_data", {})
    if not event_data:
        return "Approved, but I couldn't find the event details. Something went wrong."

    title = event_data.get("title", "Untitled Event")
    datetime_start = event_data.get("datetime_start")
    if datetime_start and isinstance(datetime_start, str):
        datetime_start = dt.fromisoformat(datetime_start)

    if not datetime_start:
        return f"Approved \"{title}\", but no date/time was extracted. Please add it manually."

    event_kwargs: dict = {
        "title": title,
        "source": pending_action.context.get("source", "email"),
        "type": event_data.get("event_type", "other"),
        "datetime_start": datetime_start,
        "extraction_confidence": event_data.get("confidence", 0.8),
        "confirmed_by_caregiver": True,
    }

    datetime_end = event_data.get("datetime_end")
    if datetime_end and isinstance(datetime_end, str):
        datetime_end = dt.fromisoformat(datetime_end)
    if datetime_end:
        event_kwargs["datetime_end"] = datetime_end
    if event_data.get("location"):
        event_kwargs["location"] = event_data["location"]
    if event_data.get("description"):
        event_kwargs["description"] = event_data["description"]

    source_ref = pending_action.context.get("source_ref")
    if source_ref:
        event_kwargs["source_refs"] = [source_ref]

    event = await events_dal.create_event(session, family_id, **event_kwargs)

    # Write to Google Calendar (source of truth)
    try:
        from src.actions.gcal import create_calendar_event

        gcal_ids = await create_calendar_event(session, family_id, event)
        if gcal_ids:
            logger.info("Created GCal event(s) %s for '%s'", gcal_ids, title)
    except Exception:
        logger.exception("Failed to create GCal event for '%s' — saved locally only", title)

    dt_str = datetime_start.strftime("%a %b %d, %I:%M %p")
    parts = [f"✅ Added to your calendar: *{title}*", f"{dt_str}"]
    if event_data.get("location"):
        parts.append(f"📍 {event_data['location']}")

    # Generate concise prep tips from the description — only the important ones
    description = event_data.get("description", "")
    if description:
        try:
            from src.llm import generate

            tip_prompt = f"""\
Event: {title}
Date: {dt_str}
Location: {event_data.get('location', 'TBD')}
Details: {description}

List ONLY tasks that are clearly important and specific to this event type. Examples:
- Birthday party → "Buy birthday gift"
- Sports → "Pack gear bag"
- Medical → "Bring insurance card"

Do NOT include generic advice like "confirm details", "check parking", "plan outfit", \
"get directions", or "check weather". If nothing specific is needed, return NOTHING.

Use "•" prefix."""
            tips = await generate(tip_prompt, system="You are a concise family assistant. Return only genuinely important, event-specific bullet points. If nothing specific is needed, return nothing at all.")
            if tips and tips.strip():
                parts.append("")
                parts.append("*Heads up:*")
                tip_lines = [line.strip() for line in tips.strip().split("\n") if line.strip().startswith("•")]
                parts.extend(tip_lines)
        except Exception:
            logger.debug("Could not generate prep tips for '%s'", title)

    return "\n".join(parts)


async def _handle_event_confirmation_edit(
    session: AsyncSession,
    family_id: UUID,
    pending_action: "PendingAction",
    instruction: str,
) -> str:
    """Handle edit instructions for pending event confirmations.

    Updates the event_data in context based on the user's correction
    (e.g., "it's at John's house at 7pm") and regenerates the confirmation.
    """
    from src.actions.whatsapp import send_buttons_to_family
    from src.llm import generate
    from src.utils.button_ids import encode_button_id

    event_data = pending_action.context.get("event_data", {})
    _local_today = (await _get_local_now(session, family_id)).strftime("%A, %B %d, %Y")

    # Use LLM to apply the user's edit to the event data
    system = """\
You are updating event details based on the user's correction. Return a JSON object \
with the updated event fields. Only include fields that changed or were added.

Valid fields: title, event_type, datetime_start (ISO 8601), datetime_end (ISO 8601), \
location, description

Only output the JSON. No other text."""

    prompt = f"""Current event details:
- Title: {event_data.get("title", "Unknown")}
- Date/time: {event_data.get("datetime_start", "Not set")}
- Location: {event_data.get("location", "Not specified")}
- Type: {event_data.get("event_type", "other")}
- Description: {event_data.get("description", "None")}

User's correction: {instruction}

Today's date: {_local_today}"""

    try:
        raw = await generate(prompt, system)
        text = raw.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(
                lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
            ).strip()

        updates = json.loads(text)

        # Apply updates to event_data
        for key, value in updates.items():
            if key in (
                "title", "event_type", "datetime_start", "datetime_end",
                "location", "description",
            ):
                event_data[key] = value

        # Rebuild confirmation message
        from datetime import datetime as dt

        datetime_start = event_data.get("datetime_start")
        if datetime_start and isinstance(datetime_start, str):
            try:
                dt_obj = dt.fromisoformat(datetime_start)
                dt_str = dt_obj.strftime("%a %b %d, %I:%M %p")
            except ValueError:
                dt_str = datetime_start
        else:
            dt_str = "TBD"

        parts = [f"*{event_data.get('title', 'Event')}*", f"📅 {dt_str}"]
        location = event_data.get("location")
        if location:
            parts.append(f"📍 {location}")
        else:
            parts.append("📍 No location specified")

        description = event_data.get("description", "")
        if description:
            # Show prep checklist items from description
            checklist_lines = [
                line.strip() for line in description.split("\n")
                if line.strip().startswith("☐")
            ]
            if checklist_lines:
                parts.append("")
                parts.append("*Suggested prep:*")
                parts.extend(checklist_lines)

        parts.append("\nAdd to your calendar?")
        body = "\n".join(parts)

        # Update the pending action context
        # Use deep copy to avoid in-place mutation that SQLAlchemy can't detect
        import copy
        updated_context = copy.deepcopy(pending_action.context)
        updated_context["event_data"] = event_data
        edit_history = (pending_action.edit_history or []) + [{
            "instruction": instruction,
            "timestamp": datetime.now(UTC).isoformat(),
        }]
        pending_action.edit_history = edit_history

        # Check if we were collecting missing details
        missing_fields = pending_action.context.get("missing_fields", [])
        if missing_fields:
            # Check what's still missing after the edit
            still_missing = []
            if "what time" in missing_fields and "datetime_start" not in updates:
                still_missing.append("what time")
            if "where" in missing_fields and not event_data.get("location"):
                still_missing.append("where")

            if still_missing:
                # Still incomplete — update context and ask for remaining fields
                updated_context["missing_fields"] = still_missing
                pending_action.context = updated_context
                pending_action.draft_content = body
                from sqlalchemy.orm.attributes import flag_modified
                flag_modified(pending_action, "context")
                flag_modified(pending_action, "edit_history")
                await session.flush()

                title = event_data.get("title", "the event")
                missing_q = " and ".join(still_missing)
                ask_text = f"Thanks! {missing_q.capitalize()} is *{title}*?"

                await memory_dal.store_message(
                    session, family_id=family_id,
                    content=f"Radar: {ask_text}",
                    msg_type="short_term",
                    expires_at=datetime.now(UTC) + timedelta(hours=24),
                )
                return ask_text

            # All details collected — clear missing_fields
            if "missing_fields" in updated_context:
                del updated_context["missing_fields"]

            # Update context before creating/confirming
            pending_action.context = updated_context
            pending_action.draft_content = body
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(pending_action, "context")
            flag_modified(pending_action, "edit_history")
            await session.flush()

            if updated_context.get("source") == "manual":
                # Manual event: auto-add directly, no confirmation needed
                response = await _create_event_from_pending(session, family_id, pending_action)
                await pending_dal.resolve_pending(
                    session, family_id, pending_action.id,
                    status=PendingActionStatus.approved,
                    resolved_by=pending_action.initiated_by,
                )
                return response
            else:
                # Email event: show confirmation with buttons
                pending_action.expires_at = datetime.now(UTC) + timedelta(hours=24)
                flag_modified(pending_action, "expires_at")
                await session.flush()
                # Fall through to button-sending below

        # Update pending action and show confirmation with buttons
        pending_action.context = updated_context
        pending_action.draft_content = body
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(pending_action, "context")
        flag_modified(pending_action, "edit_history")
        await session.flush()

        # Store updated confirmation in conversation memory
        await memory_dal.store_message(
            session,
            family_id=family_id,
            content=f"Radar: {body}",
            msg_type="short_term",
            expires_at=datetime.now(UTC) + timedelta(hours=24),
        )

        # Send buttons with confirmation
        buttons = [
            {"id": encode_button_id("event_confirm", str(pending_action.id), "yes"), "title": "Yes, add it"},
            {"id": encode_button_id("event_confirm", str(pending_action.id), "no"), "title": "No, skip"},
        ]
        try:
            await send_buttons_to_family(session, family_id, body, buttons)
            return ""  # Button message sent directly
        except Exception:
            logger.exception("Failed to send updated button confirmation")
            return body + "\n\nReply 'yes' to add or 'no' to skip."

    except json.JSONDecodeError:
        logger.warning("Could not parse event edit LLM response")
        return (
            "I had trouble applying that change. Could you try again? "
            "For example: 'it's at 7pm' or 'the location is John's house'"
        )
    except Exception:
        logger.exception("Event confirmation edit failed")
        return "Sorry, I couldn't apply that change. Please try again."


async def _handle_event_update(
    session: AsyncSession,
    family_id: UUID,
    intent: IntentResult,
    message: str,
    sender_id: UUID,
) -> str:
    """Handle event_update intent: update info about an existing event.

    Two-tier context strategy:
    1. Check recent conversation for context about which event the user means.
    2. If not enough context, query GCal to fuzzy-match the message against event titles/descriptions.
    """
    from src.llm import generate

    conversation_context, gcal_context = await _gather_event_context(
        session, family_id, message
    )

    if not conversation_context and not gcal_context:
        return (
            "I'd like to help update that event, but I'm not sure which one you mean. "
            "Could you specify the event name?"
        )

    # Use LLM to match the message to an event and determine the update
    _local_today = (await _get_local_now(session, family_id)).strftime("%A, %B %d, %Y")
    system = """\
You are Radar, a family calendar assistant. The user wants to update something about an existing event.

Your job:
1. Resolve any relative date references in the user's message (e.g., "tomorrow", "next week", \
"this Saturday") using today's date. This is critical for matching the correct event.
2. Figure out which event they're referring to by matching the resolved date AND description \
against the calendar events. Date match takes priority over conversation context.
3. Determine what update they want to make (e.g., mark a prep task done, add notes, change details).
4. Return a JSON response with:
   - "matched_event": the title of the event they're referring to (or null if you can't determine it)
   - "gcal_id": the gcal_id of the matched event if available (or null)
   - "update_description": a short description of the update (e.g., "Mark 'Purchase wedding gift' as done")
   - "updated_description": if the event has a description with checklist items, return the full updated description with the relevant item checked off (☐ → ☑). If no checklist, return null.
   - "confirmation_message": a friendly confirmation message to send back to the user

Only output the JSON. No other text."""

    prompt = f"""User message: {message}

Today's date: {_local_today}

Recent conversation:
{conversation_context if conversation_context else "(No recent conversation)"}

Upcoming calendar events:
{gcal_context if gcal_context else "(No events found)"}"""

    try:
        raw = await generate(prompt, system)
        # Parse the JSON response
        text = raw.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:]).strip()

        data = json.loads(text)

        matched_event = data.get("matched_event")
        if not matched_event:
            return (
                "I couldn't figure out which event you're referring to. "
                "Could you mention the event name?"
            )

        # If we have an updated description and a gcal_id, push the update to GCal
        updated_description = data.get("updated_description")
        gcal_id = data.get("gcal_id")

        if updated_description and gcal_id:
            try:
                from src.actions.gcal import list_upcoming_events
                from src.auth.google_client import get_calendar_service, get_google_credentials
                from src.state import families as families_dal

                caregivers = await families_dal.get_caregivers_for_family(session, family_id)
                for caregiver in caregivers:
                    if caregiver.google_refresh_token_encrypted is None:
                        continue
                    try:
                        credentials = await get_google_credentials(session, caregiver.id)
                        service = get_calendar_service(credentials)
                        service.events().patch(
                            calendarId="primary",
                            eventId=gcal_id,
                            body={"description": updated_description},
                        ).execute()
                        logger.info(
                            "Updated GCal event %s description for caregiver %s",
                            gcal_id, caregiver.id,
                        )
                        break  # Only need to update on one calendar
                    except Exception:
                        logger.debug(
                            "Could not update GCal event %s for caregiver %s",
                            gcal_id, caregiver.id,
                        )
            except Exception:
                logger.exception("Failed to push event update to GCal")

        # Also update local DB event if it exists
        try:
            local_events = await events_dal.get_upcoming_events(session, family_id, days=30)
            for ev in local_events:
                if ev.title and matched_event.lower() in ev.title.lower():
                    if updated_description:
                        ev.description = updated_description
                        await session.flush()
                        logger.info("Updated local event '%s' description", ev.title)
                    break
        except Exception:
            logger.debug("Could not update local event for '%s'", matched_event)

        confirmation = data.get("confirmation_message", f"Updated *{matched_event}* ✓")
        return confirmation

    except json.JSONDecodeError:
        logger.warning("Could not parse event_update LLM response: %s", raw[:200] if raw else "empty")
        return (
            "I understood you want to update an event, but I had trouble processing it. "
            "Could you try again with more detail?"
        )
    except Exception:
        logger.exception("Event update handler failed")
        return "Sorry, I couldn't process that update. Please try again."


async def _handle_set_preference(
    session: AsyncSession,
    family_id: UUID,
    intent: IntentResult,
    message: str,
    sender_id: UUID,
) -> str:
    """Handle preference-setting messages like 'Don't message me before 7am'."""
    from pydantic import BaseModel, Field

    class ExtractedPreference(BaseModel):
        category: str = Field(
            description="One of: pref_communication, pref_scheduling, pref_notification, pref_prep, pref_delegation, pref_decision"
        )
        fact: str = Field(description="The preference as a clear statement")
        structured_key: str | None = Field(
            default=None,
            description="If this maps to a structured setting, one of: quiet_hours_start, quiet_hours_end, delegation_areas. Otherwise null.",
        )
        structured_value: str | None = Field(
            default=None,
            description="The value for the structured setting (e.g., '07:00' for quiet_hours_start, 'school,sports' for delegation_areas). Otherwise null.",
        )

    try:
        extracted = await extract(
            prompt=f"User message: {message}",
            system=(
                "Extract the preference being set by this caregiver. "
                "Determine the category and express the preference as a clear, reusable statement. "
                "If it maps to a structured setting (quiet hours or delegation areas), extract those values too.\n"
                "quiet_hours_start/end should be in HH:MM format (24h).\n"
                "delegation_areas should be comma-separated areas like 'school,sports,medical'."
            ),
            schema=ExtractedPreference,
        )

        # Handle structured preferences
        if extracted.structured_key and extracted.structured_value:
            from datetime import time as time_type

            if extracted.structured_key in ("quiet_hours_start", "quiet_hours_end"):
                parts = extracted.structured_value.split(":")
                t = time_type(int(parts[0]), int(parts[1]))
                await pref_dal.update_preference(
                    session, sender_id, family_id,
                    **{extracted.structured_key: t},
                )
            elif extracted.structured_key == "delegation_areas":
                areas = [a.strip() for a in extracted.structured_value.split(",")]
                await pref_dal.update_preference(
                    session, sender_id, family_id,
                    delegation_areas=areas,
                )

        # Always store as freeform learning too (for prompt context)
        await learning_dal.create_learning(
            session,
            family_id=family_id,
            category=extracted.category,
            fact=extracted.fact,
            source="Stated by caregiver in conversation",
            confidence=1.0,
            caregiver_id=sender_id,
            confirmed=True,  # Explicit statements are immediately confirmed
        )

        return f"Got it — I'll remember that. ({extracted.fact})"

    except Exception:
        logger.exception("set_preference handler failed")
        return "Got it, I'll keep that in mind."


async def _handle_correct_learning(
    session: AsyncSession,
    family_id: UUID,
    intent: IntentResult,
    message: str,
    sender_id: UUID,
) -> str:
    """Handle corrections like 'Actually Emma goes to Washington Elementary'."""
    from pydantic import BaseModel, Field

    # Get current confirmed learnings to find what's being corrected
    current_learnings = await learning_dal.get_confirmed_learnings(session, family_id)

    learnings_text = "\n".join(
        f"- [id={le.id}] {le.fact} (category: {le.category})"
        for le in current_learnings
    ) if current_learnings else "(no current learnings)"

    # Also check structured data
    kids = await children_dal.get_children_for_family(session, family_id)
    kids_text = "\n".join(
        f"- {c.name}: school={c.school}, activities={c.activities}"
        for c in kids
    ) if kids else "(no children)"

    class CorrectionMatch(BaseModel):
        target_learning_id: str | None = Field(
            default=None,
            description="The UUID of the learning being corrected, if it matches one from the list. Otherwise null.",
        )
        target_structured_field: str | None = Field(
            default=None,
            description="If correcting a structured field, one of: child_school, child_activity. Otherwise null.",
        )
        target_child_name: str | None = Field(
            default=None,
            description="The child's name if the correction is about a child. Otherwise null.",
        )
        corrected_fact: str = Field(
            description="The new, corrected fact as a clear statement",
        )
        corrected_value: str | None = Field(
            default=None,
            description="The extracted value only (e.g., just the school name 'Washington Elementary', "
            "not the full sentence). Used for structured field updates.",
        )

    try:
        matched = await extract(
            prompt=f"User correction: {message}",
            system=(
                "The caregiver is correcting something Radar learned. Match the correction to "
                "either an existing learning or a structured field.\n\n"
                f"Current learnings:\n{learnings_text}\n\n"
                f"Current children data:\n{kids_text}\n\n"
                "If the correction matches a learning by ID, set target_learning_id. "
                "If it's correcting a child's school or activity, set target_structured_field and target_child_name. "
                "When setting a structured field, also set corrected_value to just the value "
                "(e.g., 'Washington Elementary' not 'Emma goes to Washington Elementary')."
            ),
            schema=CorrectionMatch,
        )

        old_fact = None

        # Handle structured field correction
        if matched.target_structured_field and matched.target_child_name:
            child = await children_dal.fuzzy_match_child(
                session, family_id, matched.target_child_name
            )
            if child:
                if matched.target_structured_field == "child_school":
                    old_fact = f"{child.name}'s school: {child.school}"
                    child.school = matched.corrected_value or matched.corrected_fact
                    await session.flush()
                elif matched.target_structured_field == "child_activity":
                    # Activity corrections are complex (add/remove/replace) — store as learning
                    logger.info("Activity correction for %s: %s", child.name, matched.corrected_fact)

        # Handle freeform learning correction
        if matched.target_learning_id:
            from uuid import UUID as UUIDType
            try:
                learning_id = UUIDType(matched.target_learning_id)
                old_learning = await session.get(learning_dal.FamilyLearning, learning_id)
                if old_learning and old_learning.family_id == family_id:
                    old_fact = old_learning.fact
                    await learning_dal.supersede_learning(
                        session, learning_id, family_id, matched.corrected_fact,
                        source="Corrected by caregiver in conversation",
                    )
            except (ValueError, Exception):
                logger.warning("Could not match learning ID: %s", matched.target_learning_id)

        if old_fact:
            return f"Updated — {old_fact} → {matched.corrected_fact}"
        else:
            # Store as a new confirmed learning
            await learning_dal.create_learning(
                session,
                family_id=family_id,
                category="child_school",  # default, will be refined
                fact=matched.corrected_fact,
                source="Corrected by caregiver in conversation",
                confidence=1.0,
                confirmed=True,
            )
            return f"Got it — {matched.corrected_fact}"

    except Exception:
        logger.exception("correct_learning handler failed")
        return "Sorry, I couldn't process that correction. Could you try rephrasing?"


async def _handle_general_question(
    session: AsyncSession,
    family_id: UUID,
    intent: IntentResult,
    message: str,
    sender_id: UUID,
) -> str:
    """Handle general questions using family context."""
    from src.agents.context import build_family_context

    try:
        ctx = await build_family_context(session, family_id, caregiver_id=sender_id)
        logger.info(
            "general_question context for family %s: children=%s, caregivers=%s, learnings=%d, prefs=%d",
            family_id,
            ctx.get("children_names"),
            ctx.get("caregiver_names"),
            len(ctx.get("learnings", [])),
            len(ctx.get("preferences", [])),
        )
        logger.debug("general_question family_context:\n%s", ctx["family_context"])
    except Exception:
        logger.exception("Failed to build family context for general_question")
        ctx = {"family_context": "(no family data available)"}

    system = (
        "You are Radar, a friendly WhatsApp assistant that helps families coordinate "
        "kids' activities. Answer the user's question helpfully and concisely.\n\n"
        "If the question is about your capabilities, explain that you can: "
        "manage calendars, track events, handle RSVPs, coordinate transport, "
        "and send reminders.\n\n"
        f"Here is what you know about this family:\n{ctx['family_context']}"
    )
    try:
        return await generate(message, system)
    except Exception:
        return "I'm here to help with your family's schedule and activities. What can I help you with?"


async def _handle_greeting(
    session: AsyncSession,
    family_id: UUID,
    intent: IntentResult,
    message: str,
    sender_id: UUID,
) -> str:
    """Handle greetings."""
    return (
        "Hi there! I'm Radar, your family activity assistant. "
        "You can tell me about events, ask about your schedule, "
        "or just let me know how I can help."
    )


async def _handle_unknown(
    session: AsyncSession,
    family_id: UUID,
    intent: IntentResult,
    message: str,
    sender_id: UUID,
) -> str:
    """Handle unclassified messages."""
    return (
        "I'm not quite sure what you mean. You can:\n"
        "- Tell me about an event (e.g., \"Soccer practice Saturday 10am\")\n"
        "- Ask about your schedule (e.g., \"What's this week look like?\")\n"
        "- Assign transport (e.g., \"I'll pick up Emma from soccer\")\n"
        "Or just ask me anything!"
    )
