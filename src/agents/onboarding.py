"""Conversational Onboarding Agent — max 3 exchanges.

Flow:
  1. Bot asks for kids' names/ages -> extract children info from natural language
  2. Generate OAuth links for each caregiver -> send links
  3. Confirm setup -> mark onboarding_complete

State is tracked via:
  - Family.onboarding_complete (False until done)
  - Presence of children (step 1 done)
  - Presence of caregiver Google tokens (step 2 done)
"""

import logging
from datetime import UTC, date, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from src.agents.schemas import ExtractedChild, OnboardingExtraction
from src.config import settings
from src.llm import extract
from src.state import children as children_dal
from src.state import families as families_dal
from src.state.models import Family

logger = logging.getLogger(__name__)

# ── Onboarding prompts ──────────────────────────────────────────────────

CHILDREN_EXTRACTION_SYSTEM = """\
You are extracting information about children from a parent's message during
onboarding. Extract each child's name, age (if mentioned), date of birth
(if mentioned), and any activities/sports/hobbies mentioned.

Also extract the caregiver's name if they introduce themselves.

Today's date is {today}.
"""

WELCOME_MESSAGE = (
    "Welcome to Radar! I'm your family calendar assistant. "
    "I help coordinate kids' activities, schedules, and logistics.\n\n"
    "Let's get you set up — it only takes a minute.\n\n"
    "First, tell me about your kids! What are their names and ages? "
    "Feel free to mention any activities they're into."
)

OAUTH_MESSAGE = (
    "Great, I've got {children_summary}! "
    "Now let's connect your Google Calendar so I can keep everything in sync.\n\n"
    "Please tap this link to connect your Google account:\n{oauth_url}\n\n"
    "If there's another caregiver who should be connected, "
    "have them join this chat and tap the link too."
)

SETUP_COMPLETE_MESSAGE = (
    "You're all set! Here's what I can help with:\n\n"
    "• \"What's on Saturday?\" — check the schedule\n"
    "• \"Add soccer practice Tuesday at 4pm\" — add events\n"
    "• \"I'll take Emma\" — claim transport duty\n\n"
    "I'll also send you a daily digest each morning and a weekly summary. "
    "Just message me anytime!"
)

WAITING_FOR_OAUTH_MESSAGE = (
    "I'm still waiting for you to connect your Google Calendar. "
    "Tap this link when you're ready:\n{oauth_url}\n\n"
    "Or say \"skip\" to finish setup without Google Calendar "
    "(you can connect it later)."
)


# ── Public API ──────────────────────────────────────────────────────────


async def handle_onboarding_message(
    session: AsyncSession,
    family_id: UUID,
    message: str,
    sender_phone: str,
) -> str:
    """Manage the 3-step onboarding flow.

    Returns the bot's response message.
    """
    family = await families_dal.get_family(session, family_id)
    if not family:
        raise ValueError(f"Family {family_id} not found")

    if family.onboarding_complete:
        return (
            "You're already set up! Just message me with any calendar "
            "questions or events to add."
        )

    # Determine current step based on state
    step = await _determine_step(session, family_id, family)

    if step == 1:
        return await _handle_step_1(session, family_id, message, sender_phone)
    elif step == 2:
        return await _handle_step_2(session, family_id, family, message, sender_phone)
    elif step == 3:
        return await _handle_step_3(session, family_id, family)
    else:
        return WELCOME_MESSAGE


async def get_welcome_message() -> str:
    """Return the initial welcome message for a new family."""
    return WELCOME_MESSAGE


# ── Step handlers ───────────────────────────────────────────────────────


async def _determine_step(
    session: AsyncSession,
    family_id: UUID,
    family: Family,
) -> int:
    """Determine which onboarding step we're on based on state."""
    children = await children_dal.get_children_for_family(session, family_id)
    caregivers = await families_dal.get_caregivers_for_family(session, family_id)
    has_oauth = any(c.google_refresh_token_encrypted for c in caregivers)

    if not children:
        return 1  # Need children info
    elif not has_oauth:
        return 2  # Need Google Calendar connection
    else:
        return 3  # Ready to complete


async def _handle_step_1(
    session: AsyncSession,
    family_id: UUID,
    message: str,
    sender_phone: str,
) -> str:
    """Step 1: Extract children info from natural language."""
    today = datetime.now(UTC).strftime("%Y-%m-%d")

    system = CHILDREN_EXTRACTION_SYSTEM.format(today=today)

    extraction = await extract(
        prompt=message,
        system=system,
        schema=OnboardingExtraction,
    )

    if not extraction.children:
        return (
            "I didn't catch any kids' names there. Could you tell me "
            "your children's names and ages? For example: "
            "\"Emma is 8 and Jake is 6, they both do soccer.\""
        )

    # Create children in the database
    created_children = []
    for child_info in extraction.children:
        dob = child_info.date_of_birth
        if not dob and child_info.age:
            # Approximate DOB from age
            today_date = date.today()
            dob = today_date.replace(year=today_date.year - child_info.age)

        child = await children_dal.create_child(
            session,
            family_id,
            name=child_info.name,
            date_of_birth=dob,
        )
        # Update activities if mentioned
        if child_info.activities:
            child.activities = child_info.activities
            await session.flush()

        created_children.append(child_info)

    # Update caregiver name if extracted
    if extraction.caregiver_name:
        caregiver = await families_dal.get_caregiver_by_phone(
            session, family_id, sender_phone
        )
        if caregiver and not caregiver.name:
            caregiver.name = extraction.caregiver_name
            await session.flush()

    # Build children summary
    children_summary = _format_children_summary(created_children)

    # Build OAuth URL for this caregiver
    oauth_url = _build_oauth_url(family_id, sender_phone)

    return OAUTH_MESSAGE.format(
        children_summary=children_summary,
        oauth_url=oauth_url,
    )


async def _handle_step_2(
    session: AsyncSession,
    family_id: UUID,
    family: Family,
    message: str,
    sender_phone: str,
) -> str:
    """Step 2: Waiting for OAuth connection."""
    message_lower = message.lower().strip()

    # Allow skipping OAuth
    if message_lower in ("skip", "skip for now", "later", "not now"):
        return await _handle_step_3(session, family_id, family)

    # Check if OAuth was completed between messages
    caregivers = await families_dal.get_caregivers_for_family(session, family_id)
    has_oauth = any(c.google_refresh_token_encrypted for c in caregivers)

    if has_oauth:
        return await _handle_step_3(session, family_id, family)

    # Still waiting — resend the OAuth link
    oauth_url = _build_oauth_url(family_id, sender_phone)
    return WAITING_FOR_OAUTH_MESSAGE.format(oauth_url=oauth_url)


async def _handle_step_3(
    session: AsyncSession,
    family_id: UUID,
    family: Family,
) -> str:
    """Step 3: Complete onboarding."""
    family.onboarding_complete = True
    family.updated_at = datetime.now(UTC)
    await session.flush()

    return SETUP_COMPLETE_MESSAGE


# ── Helpers ─────────────────────────────────────────────────────────────


def _format_children_summary(children: list[ExtractedChild]) -> str:
    """Format extracted children into a human-readable summary."""
    parts = []
    for child in children:
        part = child.name
        if child.age:
            part += f" ({child.age})"
        if child.activities:
            part += f" who does {', '.join(child.activities)}"
        parts.append(part)

    if len(parts) == 1:
        return parts[0]
    elif len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    else:
        return ", ".join(parts[:-1]) + f", and {parts[-1]}"


def _build_oauth_url(family_id: UUID, sender_phone: str) -> str:
    """Build a Google OAuth URL for the caregiver.

    Uses the config redirect URI and encodes family/phone as state.
    """
    import urllib.parse

    state = f"{family_id}:{sender_phone}"
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "response_type": "code",
        "scope": "https://www.googleapis.com/auth/calendar https://www.googleapis.com/auth/gmail.readonly",
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)
