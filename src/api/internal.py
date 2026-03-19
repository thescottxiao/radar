"""Internal API routes — triggered by Cloud Scheduler.

These endpoints are not exposed publicly. They are called by Cloud Scheduler
or internal services to trigger periodic tasks.
"""

import logging
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db import get_session
from src.state.models import Family

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal", tags=["internal"])


@router.post("/digest/daily")
async def trigger_daily_digest(
    session: AsyncSession = Depends(get_session),
):
    """Generate and send daily digests to all families.

    Called by Cloud Scheduler at each family's configured daily_digest_time.
    In practice, the scheduler calls once and this iterates all families.
    """
    from src.actions.whatsapp import send_daily_digest
    from src.agents.reminders import generate_daily_digest

    result = await session.execute(
        select(Family).where(Family.onboarding_complete.is_(True))
    )
    families = list(result.scalars().all())

    sent_count = 0
    skip_count = 0
    error_count = 0

    for family in families:
        try:
            digest = await generate_daily_digest(session, family.id)
            if digest is None:
                skip_count += 1
                continue
            await send_daily_digest(session, family.id, digest)
            sent_count += 1
        except Exception:
            logger.exception("Failed daily digest for family %s", family.id)
            error_count += 1

    logger.info(
        "Daily digest complete: sent=%d skipped=%d errors=%d",
        sent_count,
        skip_count,
        error_count,
    )
    return {
        "status": "complete",
        "sent": sent_count,
        "skipped": skip_count,
        "errors": error_count,
    }


@router.post("/digest/weekly")
async def trigger_weekly_summary(
    session: AsyncSession = Depends(get_session),
):
    """Generate and send weekly summaries to all families.

    Called by Cloud Scheduler on each family's configured weekly_summary_day.
    """
    from src.actions.whatsapp import send_weekly_summary
    from src.agents.reminders import generate_weekly_summary

    result = await session.execute(
        select(Family).where(Family.onboarding_complete.is_(True))
    )
    families = list(result.scalars().all())

    sent_count = 0
    error_count = 0

    for family in families:
        try:
            summary = await generate_weekly_summary(session, family.id)
            await send_weekly_summary(session, family.id, summary)
            sent_count += 1
        except Exception:
            logger.exception("Failed weekly summary for family %s", family.id)
            error_count += 1

    logger.info(
        "Weekly summary complete: sent=%d errors=%d",
        sent_count,
        error_count,
    )
    return {
        "status": "complete",
        "sent": sent_count,
        "errors": error_count,
    }


@router.post("/watches/renew")
async def renew_watches(
    session: AsyncSession = Depends(get_session),
):
    """Renew Gmail and GCal watches that are expiring.

    Called by Cloud Scheduler every 24 hours. Renews watches expiring within 48h.
    Gmail/GCal watches expire every 7 days; we renew on 5-day intervals.
    """
    from src.actions.gcal import renew_gcal_watch
    from src.ingestion.gmail import renew_gmail_watch
    from src.state.families import get_caregivers_needing_watch_renewal

    caregivers = await get_caregivers_needing_watch_renewal(session, within_hours=48)

    renewed_count = 0
    error_count = 0

    for caregiver in caregivers:
        try:
            # Renew Gmail watch
            if (
                caregiver.gmail_watch_expiry is None
                or caregiver.google_refresh_token_encrypted is not None
            ):
                await renew_gmail_watch(session, caregiver)
                renewed_count += 1

            # Renew GCal watch
            if (
                caregiver.gcal_watch_expiry is None
                or caregiver.google_refresh_token_encrypted is not None
            ):
                await renew_gcal_watch(session, caregiver)
                renewed_count += 1

        except Exception:
            logger.exception(
                "Failed watch renewal for caregiver %s", caregiver.id
            )
            error_count += 1

    await session.commit()

    logger.info(
        "Watch renewal complete: renewed=%d errors=%d",
        renewed_count,
        error_count,
    )
    return {
        "status": "complete",
        "renewed": renewed_count,
        "errors": error_count,
    }


# ── Test/dev endpoints ────────────────────────────────────────────────


class SimulateEmailRequest(BaseModel):
    family_id: str = "a0000000-0000-0000-0000-000000000001"
    from_address: str = "coach@lincolnsoccer.org"
    subject: str = "Soccer Tournament This Saturday"
    body: str = (
        "Hi parents,\n\n"
        "Reminder that the spring tournament is this Saturday, March 28th "
        "from 8:00 AM to 2:00 PM at Westside Sports Complex.\n\n"
        "Please make sure your child arrives by 7:30 AM for warm-ups. "
        "Each player needs to bring their own water bottle and shin guards. "
        "Lunch will NOT be provided — please pack a lunch.\n\n"
        "Also, we still need 2 parent volunteers for the snack table. "
        "Reply to this email if you can help.\n\n"
        "Go Thunder!\nCoach Williams"
    )


@router.post("/test/simulate-email")
async def simulate_email(
    request: SimulateEmailRequest = SimulateEmailRequest(),
    session: AsyncSession = Depends(get_session),
):
    """DEV ONLY: Simulate an incoming email through the extraction pipeline.

    Runs triage → extraction → persist action items/learnings → sends button
    messages for events (events are only created when the caregiver confirms).
    """
    from uuid import UUID

    from src.actions.state import persist_extraction
    from src.actions.whatsapp import send_buttons_to_family, send_to_family
    from src.extraction.email import process_email
    from src.ingestion.schemas import EmailContent
    from src.state.models import PendingActionType
    from src.state.pending import create_pending_action
    from src.utils.button_ids import encode_button_id

    family_id = UUID(request.family_id)

    email = EmailContent(
        message_id=f"test-{datetime.now().timestamp()}",
        from_address=request.from_address,
        to_addresses=["test-family@localhost"],
        subject=request.subject,
        body_text=request.body,
        date=datetime.now(),
    )

    # Run two-tier extraction pipeline
    result = await process_email(session, family_id, email)

    if not result.is_relevant:
        return {"status": "rejected", "reason": "triage classified as irrelevant"}

    # Persist action items and learnings (events go through button confirmation)
    await persist_extraction(
        session, family_id, result, source="email", source_ref=email.message_id,
        skip_events=True,
    )

    # For each extracted event: create a pending action and send Yes/No buttons
    events_pending = 0
    for ev in result.events:
        if ev.datetime_start is None:
            logger.warning("Skipping event '%s' — no datetime_start", ev.title)
            continue

        # Create pending action with event data in context
        pending = await create_pending_action(
            session,
            family_id=family_id,
            action_type=PendingActionType.event_confirmation,
            draft_content=f"{ev.title} — {ev.datetime_start.strftime('%b %d, %I:%M %p')}",
            context={
                "event_data": ev.model_dump(mode="json"),
                "email_subject": request.subject,
                "source_ref": email.message_id,
            },
        )

        # Build button message
        time_str = ev.datetime_start.strftime("%b %d, %I:%M %p")
        body = f"New event from email:\n*{ev.title}*\n{time_str}"
        if ev.location:
            body += f"\n📍 {ev.location}"
        body += "\n\nAdd to your calendar?"

        buttons = [
            {"id": encode_button_id("event_confirm", str(pending.id), "yes"), "title": "Yes, add it"},
            {"id": encode_button_id("event_confirm", str(pending.id), "no"), "title": "No, skip"},
        ]

        try:
            await send_buttons_to_family(session, family_id, body, buttons)
        except Exception:
            logger.exception("Failed to send button message for event '%s'", ev.title)

        events_pending += 1

    # Send a plain text summary for action items (if any, and no events)
    if result.action_items and not result.events:
        lines = [f"📧 *{request.subject}*", ""]
        lines.append("*Action items:*")
        for ai in result.action_items:
            due_str = f" (due {ai.due_date.strftime('%b %d')})" if ai.due_date else ""
            lines.append(f"• {ai.description}{due_str}")
        if result.email_summary:
            lines.append(f"\n_{result.email_summary}_")
        message = "\n".join(lines)
        try:
            await send_to_family(session, family_id, message)
        except Exception:
            logger.exception("Failed to send WhatsApp notification")

    await session.commit()

    return {
        "status": "processed",
        "is_relevant": result.is_relevant,
        "events_pending_confirmation": events_pending,
        "action_items": len(result.action_items),
        "learnings": len(result.learnings),
        "summary": result.email_summary,
    }
