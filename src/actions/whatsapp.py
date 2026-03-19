"""WhatsApp sending action layer.

Provides family-level message sending by looking up all caregivers
and dispatching via the low-level WhatsApp client.

Rules:
- Bot-initiated messages require approved templates.
- Free-form only within 24-hour windows.
- Template categories: new_event, reminder, deadline_alert, approval_request,
  daily_digest, weekly_summary, assignment_nudge, conflict_alert.
"""

import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from src.state import families as families_dal
from src.whatsapp_client import send_message, send_template

logger = logging.getLogger(__name__)


async def send_to_family(
    session: AsyncSession, family_id: UUID, message: str
) -> None:
    """Send a free-form text message to all active caregivers in a family.

    Only works within the 24-hour window after a caregiver-initiated message.
    """
    caregivers = await families_dal.get_caregivers_for_family(session, family_id)
    if not caregivers:
        logger.warning("No active caregivers found for family %s", family_id)
        return

    for caregiver in caregivers:
        try:
            await send_message(caregiver.whatsapp_phone, message)
            logger.info(
                "Sent message to caregiver %s (%s)",
                caregiver.id,
                caregiver.name or caregiver.whatsapp_phone,
            )
        except Exception:
            logger.exception(
                "Failed to send message to caregiver %s (%s)",
                caregiver.id,
                caregiver.whatsapp_phone,
            )


async def send_template_to_family(
    session: AsyncSession,
    family_id: UUID,
    template: str,
    params: list | None = None,
) -> None:
    """Send a template message to all active caregivers in a family.

    Works outside the 24-hour window. Template must be pre-approved.
    """
    caregivers = await families_dal.get_caregivers_for_family(session, family_id)
    if not caregivers:
        logger.warning("No active caregivers found for family %s", family_id)
        return

    # Build template components from params
    components = None
    if params:
        components = [
            {
                "type": "body",
                "parameters": [{"type": "text", "text": str(p)} for p in params],
            }
        ]

    for caregiver in caregivers:
        try:
            await send_template(
                caregiver.whatsapp_phone,
                template_name=template,
                components=components,
            )
            logger.info(
                "Sent template '%s' to caregiver %s",
                template,
                caregiver.id,
            )
        except Exception:
            logger.exception(
                "Failed to send template '%s' to caregiver %s",
                template,
                caregiver.id,
            )


async def send_daily_digest(
    session: AsyncSession, family_id: UUID, content: str
) -> None:
    """Send the daily digest to a family.

    Uses the daily_digest template for bot-initiated delivery.
    Falls back to free-form if template delivery fails.
    """
    try:
        await send_template_to_family(
            session,
            family_id,
            template="daily_digest",
            params=[content],
        )
    except Exception:
        logger.warning(
            "Template delivery failed for daily digest (family %s), trying free-form",
            family_id,
        )
        await send_to_family(session, family_id, content)


async def send_weekly_summary(
    session: AsyncSession, family_id: UUID, content: str
) -> None:
    """Send the weekly summary to a family.

    Uses the weekly_summary template for bot-initiated delivery.
    Falls back to free-form if template delivery fails.
    """
    try:
        await send_template_to_family(
            session,
            family_id,
            template="weekly_summary",
            params=[content],
        )
    except Exception:
        logger.warning(
            "Template delivery failed for weekly summary (family %s), trying free-form",
            family_id,
        )
        await send_to_family(session, family_id, content)
