"""Intent Router: classifies WhatsApp messages and dispatches to handlers."""

import json
import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from src.extraction.schemas import IntentResult, IntentType
from src.llm import HAIKU_MODEL, classify
from src.state import events as events_dal
from src.state import learning as learning_dal
from src.state import memory as memory_dal
from src.state import pending as pending_dal
from src.state.models import PendingActionStatus

logger = logging.getLogger(__name__)

# System prompt for intent classification
INTENT_SYSTEM_PROMPT = """\
You are the intent classifier for Radar, a WhatsApp-based family activity assistant.
Classify the user's message into exactly one intent category.

Intent categories:
- add_event: User wants to add a new event (e.g. "Emma has soccer Saturday at 10am")
- query_schedule: User is asking about upcoming events or schedule (e.g. "What's on this weekend?")
- modify_event: User wants to change an existing event (e.g. "Move soccer to 3pm")
- cancel_event: User wants to cancel an event (e.g. "Cancel the dentist appointment")
- assign_transport: User is assigning who drops off or picks up (e.g. "I'll take Emma to soccer")
- rsvp_response: User is responding to an RSVP prompt (e.g. "Yes to the birthday party")
- add_child_info: User is providing child info (e.g. "Jake's shoe size is 3")
- approval_response: User is responding to a pending action — approve, dismiss, or edit instruction
- event_update: User wants to update info about an existing event — mark a prep task done, add notes, change logistics (e.g. "I already bought the wedding gift", "I packed the swimsuit for camp")
- general_question: A general question about the assistant or non-schedule topic
- greeting: A greeting or small talk
- unknown: Cannot determine intent

IMPORTANT: If there is a pending action awaiting approval and the message looks like it could be
a response to that action (approve, reject, edit, etc.), classify as approval_response.

IMPORTANT: Consider recent conversation context when classifying. If the user references something
just discussed (e.g. they just confirmed an event and now mention a related task), classify based
on that context. For example, if they just confirmed "Garden Party for the Newlyweds" and then say
"I already bought a wedding gift", that's an event_update for the garden party, not an unknown.

Respond with a JSON object:
{
  "intent": "<intent_type>",
  "confidence": <0.0-1.0>,
  "extracted_params": {<any relevant extracted data>}
}

Only output the JSON. No other text.
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
        context_parts.append(
            f"There are {len(pending_actions)} pending action(s) awaiting approval. "
            f"Most recent: {pending_actions[0].draft_content[:100]}"
        )
    if recent_messages:
        recent_texts = [m.content for m in reversed(recent_messages[-10:])]
        context_parts.append("Recent conversation:\n" + "\n".join(recent_texts))

    context = "\n\n".join(context_parts) if context_parts else "No recent context."
    prompt = f"Context:\n{context}\n\nUser message: {message}"

    try:
        raw = await classify(prompt, INTENT_SYSTEM_PROMPT, model=HAIKU_MODEL)
        parsed = _parse_classification_response(raw)
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


# ── Intent handlers ────────────────────────────────────────────────────
# Phase 1 implementations — basic versions that will be expanded in later phases.


async def _handle_add_event(
    session: AsyncSession,
    family_id: UUID,
    intent: IntentResult,
    message: str,
    sender_id: UUID,
) -> str:
    """Handle add_event intent: extract event details and create."""
    from src.extraction.schemas import ExtractedEvent
    from src.llm import extract

    system = (
        "Extract event details from the user's message. "
        "Include title, date/time, location, and which child it's for if mentioned. "
        "Use the current date context: today is "
        + datetime.now(UTC).strftime("%A, %B %d, %Y") + "."
    )

    try:
        extracted = await extract(message, system, ExtractedEvent)
    except Exception:
        logger.exception("Event extraction failed")
        return (
            "I understood you want to add an event but couldn't extract the details. "
            "Could you try again with the date, time, and title?"
        )

    # Create the event in the database
    event_kwargs: dict = {
        "title": extracted.title,
        "source": "manual",
        "type": extracted.event_type,
        "extraction_confidence": extracted.confidence,
    }

    if extracted.datetime_start:
        event_kwargs["datetime_start"] = extracted.datetime_start
    else:
        return (
            f"Got it — \"{extracted.title}\". "
            "When is it? Please include the date and time."
        )

    if extracted.datetime_end:
        event_kwargs["datetime_end"] = extracted.datetime_end
    if extracted.location:
        event_kwargs["location"] = extracted.location
    if extracted.description:
        event_kwargs["description"] = extracted.description

    await events_dal.create_event(session, family_id, **event_kwargs)
    await session.flush()

    # Build confirmation
    dt_str = extracted.datetime_start.strftime("%A, %B %d at %I:%M %p") if extracted.datetime_start else "TBD"
    parts = [f"Added: {extracted.title}", f"When: {dt_str}"]
    if extracted.location:
        parts.append(f"Where: {extracted.location}")

    if extracted.confidence < 0.6:
        parts.append("\nDoes this look right? Reply to correct anything.")

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
    """Handle modify_event intent."""
    return (
        "I can help modify that event. "
        "Could you tell me which event and what you'd like to change?"
    )


async def _handle_cancel_event(
    session: AsyncSession,
    family_id: UUID,
    intent: IntentResult,
    message: str,
    sender_id: UUID,
) -> str:
    """Handle cancel_event intent."""
    return (
        "Which event would you like to cancel? "
        "Please give me the name or date so I can find it."
    )


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
        # Generate revised draft using LLM
        from src.llm import generate

        pending_actions = await pending_dal.get_active_pending(session, family_id)
        current_action = next(
            (a for a in pending_actions if a.id == pending_action_id), None
        )
        if not current_action:
            return "I couldn't find that pending action. It may have expired."

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
                action_id=pending_action_id,
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
        "source": "email",
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

List only the most important things to know or prepare before this event. Skip anything obvious or trivial. Each bullet should be short (max 10 words) and actionable. Use "•" prefix. No preamble."""
            tips = await generate(tip_prompt, system="You are a concise family assistant. Return only bullet points, nothing else. Only include what actually matters.")
            if tips and tips.strip():
                parts.append("")
                parts.append("*Heads up:*")
                tip_lines = [line.strip() for line in tips.strip().split("\n") if line.strip().startswith("•")]
                parts.extend(tip_lines)
        except Exception:
            logger.debug("Could not generate prep tips for '%s'", title)

    return "\n".join(parts)


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

    # Tier 1: Gather recent conversation context
    recent_messages = await memory_dal.get_recent_messages(session, family_id, limit=10)
    conversation_context = ""
    if recent_messages:
        recent_texts = [m.content for m in reversed(recent_messages[-10:])]
        conversation_context = "\n".join(recent_texts)

    # Tier 2: Fetch upcoming events from GCal for matching
    gcal_context = ""
    gcal_events: list[dict] = []
    try:
        from src.actions.gcal import list_upcoming_events

        gcal_events = await list_upcoming_events(session, family_id, days=30)
        if gcal_events:
            event_summaries = []
            for ev in gcal_events:
                summary = f"- {ev['title']} ({ev.get('start', 'TBD')}) [gcal_id: {ev.get('gcal_id', 'unknown')}]"
                if ev.get("description"):
                    desc_preview = ev["description"][:500]
                    summary += f"\n  Description: {desc_preview}"
                if ev.get("location"):
                    summary += f"\n  Location: {ev['location']}"
                event_summaries.append(summary)
            gcal_context = "\n".join(event_summaries)
    except Exception:
        logger.warning("Could not fetch GCal events for event_update context (family %s)", family_id)

    # Also check local DB events as additional fallback
    if not gcal_events:
        try:
            local_events = await events_dal.get_upcoming_events(session, family_id, days=30)
            if local_events:
                event_summaries = []
                for ev in local_events:
                    dt_str = ev.datetime_start.strftime("%a %b %d, %I:%M %p") if ev.datetime_start else "TBD"
                    summary = f"- {ev.title} ({dt_str})"
                    if ev.description:
                        summary += f"\n  Description: {ev.description[:500]}"
                    if ev.location:
                        summary += f"\n  Location: {ev.location}"
                    event_summaries.append(summary)
                gcal_context = "\n".join(event_summaries)
        except Exception:
            logger.warning("Could not fetch local events for event_update context (family %s)", family_id)

    if not conversation_context and not gcal_context:
        return (
            "I'd like to help update that event, but I'm not sure which one you mean. "
            "Could you specify the event name?"
        )

    # Use LLM to match the message to an event and determine the update
    system = """\
You are Radar, a family calendar assistant. The user wants to update something about an existing event.

Your job:
1. Figure out which event they're referring to using conversation context and calendar events.
2. Determine what update they want to make (e.g., mark a prep task done, add notes, change details).
3. Return a JSON response with:
   - "matched_event": the title of the event they're referring to (or null if you can't determine it)
   - "gcal_id": the gcal_id of the matched event if available (or null)
   - "update_description": a short description of the update (e.g., "Mark 'Purchase wedding gift' as done")
   - "updated_description": if the event has a description with checklist items, return the full updated description with the relevant item checked off (☐ → ☑). If no checklist, return null.
   - "confirmation_message": a friendly confirmation message to send back to the user

Only output the JSON. No other text."""

    prompt = f"""User message: {message}

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


async def _handle_general_question(
    session: AsyncSession,
    family_id: UUID,
    intent: IntentResult,
    message: str,
    sender_id: UUID,
) -> str:
    """Handle general questions using LLM."""
    from src.llm import generate

    system = (
        "You are Radar, a friendly WhatsApp assistant that helps families coordinate "
        "kids' activities. Answer the user's question helpfully and concisely. "
        "If the question is about your capabilities, explain that you can: "
        "manage calendars, track events, handle RSVPs, coordinate transport, "
        "and send reminders."
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
