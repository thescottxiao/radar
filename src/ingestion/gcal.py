"""GCal Webhook Handler — processes Google Calendar push notifications.

Flow:
  1. Extract X-Goog-Channel-ID -> look up caregiver by gcal_watch_channel_id
  2. If X-Goog-Resource-State is "sync" -> acknowledge, return
  3. Fetch changed events via Calendar API with syncToken
  4. For each changed event, pass to Calendar Change Detector
  5. Update caregiver's gcal_sync_token
  6. Send WhatsApp notifications for relevant changes
"""

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.extraction.calendar import process_calendar_change
from src.state import families as families_dal
from src.state.models import Caregiver
from src.whatsapp_client import send_message

logger = logging.getLogger(__name__)


async def handle_gcal_notification(
    session: AsyncSession,
    headers: dict[str, str],
    body: bytes | dict | None = None,
) -> None:
    """Process a GCal push notification webhook.

    Args:
        session: Database session.
        headers: HTTP headers from the webhook request. Expected headers:
            - X-Goog-Channel-ID: The channel ID we set when creating the watch
            - X-Goog-Resource-State: "sync", "exists", or "not_exists"
            - X-Goog-Resource-ID: The Google resource ID
        body: Webhook body (usually empty for GCal push).
    """
    channel_id = headers.get("X-Goog-Channel-ID") or headers.get("x-goog-channel-id")
    resource_state = (
        headers.get("X-Goog-Resource-State") or headers.get("x-goog-resource-state")
    )

    if not channel_id:
        logger.warning("GCal webhook missing X-Goog-Channel-ID header")
        return

    logger.info(
        "GCal notification: channel=%s state=%s",
        channel_id,
        resource_state,
    )

    # Step 1: Look up caregiver by channel ID
    caregiver = await _get_caregiver_by_channel(session, channel_id)
    if not caregiver:
        logger.warning(
            "No caregiver found for GCal channel_id=%s — stale watch?",
            channel_id,
        )
        return

    # Step 2: Handle sync acknowledgement
    if resource_state == "sync":
        logger.info(
            "GCal sync acknowledgement for caregiver=%s", caregiver.id
        )
        return

    # Step 3: Fetch changed events from Google Calendar API
    try:
        from src.actions.gcal import fetch_calendar_changes

        changes = await fetch_calendar_changes(
            session,
            caregiver.id,
            sync_token=caregiver.gcal_sync_token,
        )
    except ImportError:
        logger.error(
            "src.actions.gcal not available — cannot fetch calendar changes"
        )
        return
    except Exception as exc:
        logger.error(
            "Failed to fetch calendar changes for caregiver=%s: %s",
            caregiver.id,
            exc,
        )
        return

    if not changes:
        logger.info("No calendar changes for caregiver=%s", caregiver.id)
        return

    changed_events = changes.get("events", [])
    new_sync_token = changes.get("next_sync_token")

    # Step 4: Process each changed event through the Calendar Change Detector
    notifications: list[str] = []

    for gcal_event in changed_events:
        try:
            result = await process_calendar_change(
                session,
                caregiver.family_id,
                gcal_event,
                caregiver.id,
            )
            if result and result.get("notification"):
                notifications.append(result["notification"])
        except Exception as exc:
            logger.error(
                "Error processing calendar change for event %s: %s",
                gcal_event.get("id", "unknown"),
                exc,
            )

    # Step 5: Update caregiver's sync token
    if new_sync_token:
        caregiver.gcal_sync_token = new_sync_token
        await session.flush()
        logger.info(
            "Updated sync token for caregiver=%s", caregiver.id
        )

    # Step 6: Send WhatsApp notifications for relevant changes
    if notifications:
        await _send_change_notifications(
            session, caregiver.family_id, notifications
        )


# ── Private helpers ─────────────────────────────────────────────────────


async def _get_caregiver_by_channel(
    session: AsyncSession,
    channel_id: str,
) -> Caregiver | None:
    """Look up a caregiver by their GCal watch channel ID."""
    result = await session.execute(
        select(Caregiver).where(
            Caregiver.gcal_watch_channel_id == channel_id,
            Caregiver.is_active.is_(True),
        )
    )
    return result.scalar_one_or_none()


async def _send_change_notifications(
    session: AsyncSession,
    family_id: UUID,
    notifications: list[str],
) -> None:
    """Send WhatsApp notifications about calendar changes to all caregivers."""
    caregivers = await families_dal.get_caregivers_for_family(session, family_id)

    combined = "Calendar update:\n" + "\n".join(f"• {n}" for n in notifications)

    for caregiver in caregivers:
        try:
            await send_message(caregiver.whatsapp_phone, combined)
        except Exception as exc:
            logger.error(
                "Failed to send notification to %s: %s",
                caregiver.whatsapp_phone,
                exc,
            )
