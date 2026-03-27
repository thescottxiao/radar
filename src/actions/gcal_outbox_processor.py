"""Background processor for the GCal outbox.

Polls the gcal_outbox table for pending items, dispatches them to the
appropriate GCal API functions, and handles success/failure/retry.
"""

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.state.models import GcalOutboxItem, GcalOutboxOperation

logger = logging.getLogger(__name__)

# Poll interval in seconds
POLL_INTERVAL = 5


async def process_outbox_loop() -> None:
    """Long-running loop that processes pending GCal outbox items."""
    from src.db import async_session_factory

    logger.info("GCal outbox processor started (poll every %ds)", POLL_INTERVAL)
    while True:
        try:
            # Claim items in one transaction
            from src.state import outbox as outbox_dal

            item_ids = []
            async with async_session_factory() as session:
                async with session.begin():
                    items = await outbox_dal.claim_pending_items(session, batch_size=10)
                    item_ids = [item.id for item in items]
                    if item_ids:
                        logger.info("Outbox claimed %d items: %s", len(item_ids), [(str(i.id)[:8], str(i.operation)) for i in items])
                    else:
                        logger.debug("Outbox poll: 0 pending items")

            # Process each item in its own transaction for isolation
            for item_id in item_ids:
                try:
                    async with async_session_factory() as session:
                        async with session.begin():
                            result = await session.execute(
                                select(GcalOutboxItem).where(GcalOutboxItem.id == item_id)
                            )
                            item = result.scalar_one_or_none()
                            if item:
                                await _process_item(session, item)
                except Exception:
                    logger.exception("Failed to process outbox item %s", item_id)
        except asyncio.CancelledError:
            logger.info("GCal outbox processor shutting down")
            raise
        except Exception:
            logger.exception("Outbox processor loop error")

        await asyncio.sleep(POLL_INTERVAL)


async def _process_item(session: AsyncSession, item: GcalOutboxItem) -> None:
    """Process a single outbox item. Marks it done or failed."""
    from src.state import outbox as outbox_dal

    try:
        if item.operation == GcalOutboxOperation.create:
            await _handle_create(session, item)
        elif item.operation == GcalOutboxOperation.update:
            await _handle_update(session, item)
        elif item.operation == GcalOutboxOperation.patch:
            await _handle_patch(session, item)
        elif item.operation == GcalOutboxOperation.delete:
            await _handle_delete(session, item)
        else:
            logger.warning("Unknown outbox operation: %s", item.operation)
            await outbox_dal.mark_failed(session, item, f"Unknown operation: {item.operation}")
            return

        await outbox_dal.mark_done(session, item)
    except Exception as exc:
        await outbox_dal.mark_failed(session, item, str(exc))


async def _handle_create(session: AsyncSession, item: GcalOutboxItem) -> None:
    """Process a 'create' outbox item."""
    from src.actions.gcal import create_calendar_event
    from src.state import events as events_dal
    from src.state import outbox as outbox_dal

    if not item.event_id:
        logger.warning("Create outbox item %s has no event_id, marking done", item.id)
        await outbox_dal.mark_done(session, item)
        return

    event = await events_dal.get_event(session, item.family_id, item.event_id)
    if not event:
        logger.warning("Event %s not found for outbox item %s, marking done", item.event_id, item.id)
        await outbox_dal.mark_done(session, item)
        return

    # Skip cancelled events
    if event.cancelled_at is not None:
        logger.info("Event %s is cancelled, skipping create", item.event_id)
        await outbox_dal.mark_done(session, item)
        return

    # Idempotency: skip if event already has GCal refs
    existing_gcal_refs = [r for r in (event.source_refs or []) if r.startswith("gcal:")]
    if existing_gcal_refs:
        logger.info(
            "Event %s already has GCal refs %s, skipping create",
            item.event_id, existing_gcal_refs,
        )
        return

    logger.info("Outbox creating GCal event for %s ('%s')", item.event_id, event.title)
    gcal_ids = await create_calendar_event(session, item.family_id, event)
    if gcal_ids:
        logger.info("Outbox created GCal event(s) %s for event %s", gcal_ids, item.event_id)
    else:
        logger.warning("Outbox create returned no GCal IDs for event %s — GCal write may have silently failed", item.event_id)


async def _handle_update(session: AsyncSession, item: GcalOutboxItem) -> None:
    """Process an 'update' outbox item."""
    from src.actions.gcal import update_calendar_event
    from src.state import events as events_dal
    from src.state import outbox as outbox_dal

    if not item.event_id:
        logger.warning("Update outbox item %s has no event_id, marking done", item.id)
        await outbox_dal.mark_done(session, item)
        return

    event = await events_dal.get_event(session, item.family_id, item.event_id)
    if not event:
        logger.warning("Event %s not found for outbox item %s, marking done", item.event_id, item.id)
        await outbox_dal.mark_done(session, item)
        return

    # Skip cancelled events
    if event.cancelled_at is not None:
        logger.info("Event %s is cancelled, skipping update", item.event_id)
        await outbox_dal.mark_done(session, item)
        return

    await update_calendar_event(session, item.family_id, event)
    logger.info("Outbox updated GCal event for event %s", item.event_id)


async def _handle_patch(session: AsyncSession, item: GcalOutboxItem) -> None:
    """Process a 'patch' outbox item."""
    from src.actions.gcal import patch_calendar_event

    gcal_id = item.gcal_event_id
    if not gcal_id:
        logger.warning("Patch outbox item %s has no gcal_event_id, skipping", item.id)
        return

    await patch_calendar_event(session, item.family_id, gcal_id, item.payload)
    logger.info("Outbox patched GCal event %s", gcal_id)


async def _handle_delete(session: AsyncSession, item: GcalOutboxItem) -> None:
    """Process a 'delete' outbox item."""
    from src.actions.gcal import delete_calendar_event, delete_gcal_event_by_id

    gcal_id = item.gcal_event_id
    if gcal_id:
        await delete_gcal_event_by_id(session, item.family_id, gcal_id)
        logger.info("Outbox deleted GCal event %s", gcal_id)
    elif item.event_id:
        from src.state import events as events_dal

        event = await events_dal.get_event(session, item.family_id, item.event_id)
        if event:
            await delete_calendar_event(session, item.family_id, event)
            logger.info("Outbox deleted GCal event for event %s", item.event_id)
