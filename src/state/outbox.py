"""Data access layer for the GCal outbox (async write queue with retry)."""

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.state.models import GcalOutboxItem, GcalOutboxOperation, GcalOutboxStatus

logger = logging.getLogger(__name__)

# Exponential backoff schedule (seconds): 30s, 2m, 8m, 32m, 2h
_BACKOFF_SECONDS = [30, 120, 480, 1920, 7200]


async def enqueue_gcal_write(
    session: AsyncSession,
    family_id: UUID,
    event_id: UUID | None,
    operation: GcalOutboxOperation,
    payload: dict,
    idempotency_key: str,
    gcal_event_id: str | None = None,
) -> GcalOutboxItem | None:
    """Enqueue a GCal write operation. Returns the item, or None if duplicate (idempotent).

    Uses INSERT ... ON CONFLICT DO NOTHING to prevent duplicate enqueues.
    """
    stmt = (
        insert(GcalOutboxItem)
        .values(
            family_id=family_id,
            event_id=event_id,
            operation=operation,
            payload=payload,
            gcal_event_id=gcal_event_id,
            idempotency_key=idempotency_key,
        )
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
        .returning(GcalOutboxItem)
    )
    result = await session.execute(stmt)
    row = result.scalar_one_or_none()
    if row:
        logger.info("Enqueued GCal %s for event %s (key=%s)", operation, event_id, idempotency_key)
    else:
        logger.debug("Duplicate outbox entry skipped (key=%s)", idempotency_key)
    return row


async def claim_pending_items(
    session: AsyncSession, batch_size: int = 10
) -> list[GcalOutboxItem]:
    """Claim pending outbox items for processing.

    Uses SELECT ... FOR UPDATE SKIP LOCKED to safely handle concurrent processors.
    """
    now = datetime.now(UTC)
    stmt = (
        select(GcalOutboxItem)
        .where(
            GcalOutboxItem.status.in_([GcalOutboxStatus.pending, GcalOutboxStatus.failed]),
            GcalOutboxItem.next_retry_at <= now,
        )
        .order_by(GcalOutboxItem.created_at)
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    )
    result = await session.execute(stmt)
    items = list(result.scalars().all())

    # Mark as processing
    for item in items:
        item.status = GcalOutboxStatus.processing
    await session.flush()

    return items


async def mark_done(session: AsyncSession, item: GcalOutboxItem) -> None:
    """Mark an outbox item as successfully processed."""
    item.status = GcalOutboxStatus.done
    item.processed_at = datetime.now(UTC)
    await session.flush()


async def mark_failed(
    session: AsyncSession, item: GcalOutboxItem, error: str
) -> None:
    """Mark an outbox item as failed with exponential backoff retry."""
    item.last_error = error[:2000]  # Truncate long errors
    item.retry_count += 1

    if item.retry_count >= item.max_retries:
        item.status = GcalOutboxStatus.dead
        logger.warning(
            "Outbox item %s is dead after %d retries: %s",
            item.id, item.retry_count, error[:200],
        )
    else:
        item.status = GcalOutboxStatus.failed
        backoff_idx = min(item.retry_count - 1, len(_BACKOFF_SECONDS) - 1)
        backoff = _BACKOFF_SECONDS[backoff_idx]
        item.next_retry_at = datetime.now(UTC) + timedelta(seconds=backoff)
        logger.info(
            "Outbox item %s failed (retry %d/%d), next retry in %ds: %s",
            item.id, item.retry_count, item.max_retries, backoff, error[:200],
        )

    await session.flush()


async def cancel_pending_for_event(
    session: AsyncSession, family_id: UUID, event_id: UUID
) -> int:
    """Mark all pending/failed/processing outbox items for an event as done.

    Called when an event is cancelled to prevent the outbox processor from
    re-creating or updating the event in GCal.
    Returns the count of items cancelled.
    """
    stmt = (
        update(GcalOutboxItem)
        .where(
            GcalOutboxItem.family_id == family_id,
            GcalOutboxItem.event_id == event_id,
            GcalOutboxItem.status.in_([
                GcalOutboxStatus.pending,
                GcalOutboxStatus.failed,
                GcalOutboxStatus.processing,
            ]),
        )
        .values(status=GcalOutboxStatus.done, processed_at=datetime.now(UTC))
    )
    result = await session.execute(stmt)
    count = result.rowcount
    if count:
        logger.info("Cancelled %d pending outbox items for event %s", count, event_id)
    return count


async def get_dead_items(
    session: AsyncSession, family_id: UUID | None = None
) -> list[GcalOutboxItem]:
    """Get dead-letter outbox items for monitoring."""
    stmt = select(GcalOutboxItem).where(GcalOutboxItem.status == GcalOutboxStatus.dead)
    if family_id:
        stmt = stmt.where(GcalOutboxItem.family_id == family_id)
    stmt = stmt.order_by(GcalOutboxItem.created_at.desc())
    result = await session.execute(stmt)
    return list(result.scalars().all())
