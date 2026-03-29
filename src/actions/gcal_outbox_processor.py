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
    """Process a 'create' outbox item — for events or todos."""
    from src.actions.gcal import create_calendar_event
    from src.state import events as events_dal
    from src.state import outbox as outbox_dal
    from src.state import todos as todos_dal

    # Handle todo creates
    if item.todo_id:
        await _handle_todo_create(session, item)
        return

    if not item.event_id:
        logger.warning("Create outbox item %s has no event_id or todo_id, marking done", item.id)
        await outbox_dal.mark_done(session, item)
        return

    event = await events_dal.get_event(session, item.family_id, item.event_id)
    if not event:
        logger.warning("Event %s not found for outbox item %s, marking done", item.event_id, item.id)
        await outbox_dal.mark_done(session, item)
        return

    # Skip cancelled events
    if event.description and "[CANCELLED]" in event.description:
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

    gcal_ids = await create_calendar_event(session, item.family_id, event)
    if gcal_ids:
        logger.info("Outbox created GCal event(s) %s for event %s", gcal_ids, item.event_id)


async def _handle_todo_create(session: AsyncSession, item: GcalOutboxItem) -> None:
    """Create a GCal calendar entry for a todo at its deadline date."""
    from src.actions.gcal import todo_to_gcal_body
    from src.auth.google_client import get_calendar_service, get_google_credentials
    from src.state import events as events_dal
    from src.state import families as families_dal
    from src.state import outbox as outbox_dal
    from src.state import todos as todos_dal

    todo = await todos_dal.get_todo(session, item.family_id, item.todo_id)
    if not todo:
        logger.warning("Todo %s not found for outbox item %s, marking done", item.todo_id, item.id)
        await outbox_dal.mark_done(session, item)
        return

    if not todo.due_date:
        logger.info("Todo %s has no due_date, skipping GCal create", item.todo_id)
        await outbox_dal.mark_done(session, item)
        return

    # Idempotency: skip if todo already has a GCal event ID
    if todo.gcal_event_id:
        logger.info("Todo %s already has gcal_event_id %s, skipping", item.todo_id, todo.gcal_event_id)
        return

    # Get family timezone
    family = await families_dal.get_family(session, item.family_id)
    family_tz = family.timezone if family else "America/New_York"

    # Get linked event title if available
    linked_event_title = None
    if todo.event_id:
        event = await events_dal.get_event(session, item.family_id, todo.event_id)
        if event:
            linked_event_title = event.title

    body = todo_to_gcal_body(todo, linked_event_title=linked_event_title, family_timezone=family_tz)

    caregivers = await families_dal.get_caregivers_for_family(session, item.family_id)
    for caregiver in caregivers:
        if caregiver.google_refresh_token_encrypted is None:
            continue
        try:
            credentials = await get_google_credentials(session, caregiver.id)
            service = get_calendar_service(credentials)
            result = service.events().insert(calendarId="primary", body=body).execute()
            gcal_event_id = result.get("id", "")
            todo.gcal_event_id = gcal_event_id
            await session.flush()
            logger.info("Created GCal entry %s for todo %s", gcal_event_id, item.todo_id)
            break  # Only need to create on one calendar
        except Exception:
            logger.exception("Failed to create GCal entry for todo %s on caregiver %s", item.todo_id, caregiver.id)


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
    if event.description and "[CANCELLED]" in event.description:
        logger.info("Event %s is cancelled, skipping update", item.event_id)
        await outbox_dal.mark_done(session, item)
        return

    await update_calendar_event(session, item.family_id, event)


async def _handle_patch(session: AsyncSession, item: GcalOutboxItem) -> None:
    """Process a 'patch' outbox item."""
    from src.actions.gcal import patch_calendar_event

    gcal_id = item.gcal_event_id
    if not gcal_id:
        logger.warning("Patch outbox item %s has no gcal_event_id, skipping", item.id)
        return

    await patch_calendar_event(session, item.family_id, gcal_id, item.payload)


async def _handle_delete(session: AsyncSession, item: GcalOutboxItem) -> None:
    """Process a 'delete' outbox item — for events or todos."""
    from src.actions.gcal import delete_calendar_event, delete_gcal_event_by_id

    # Handle todo deletes
    if item.todo_id:
        from src.state import todos as todos_dal

        todo = await todos_dal.get_todo(session, item.family_id, item.todo_id)
        if todo and todo.gcal_event_id:
            await delete_gcal_event_by_id(session, item.family_id, todo.gcal_event_id)
            todo.gcal_event_id = None
            await session.flush()
        return

    gcal_id = item.gcal_event_id
    if gcal_id:
        await delete_gcal_event_by_id(session, item.family_id, gcal_id)
    elif item.event_id:
        from src.state import events as events_dal

        event = await events_dal.get_event(session, item.family_id, item.event_id)
        if event:
            await delete_calendar_event(session, item.family_id, event)
