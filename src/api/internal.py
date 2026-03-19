"""Internal API routes — triggered by Cloud Scheduler.

These endpoints are not exposed publicly. They are called by Cloud Scheduler
or internal services to trigger periodic tasks.
"""

import logging

from fastapi import APIRouter, Depends
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

            # GCal watch renewal would go here (Phase 1 already handles GCal)

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
