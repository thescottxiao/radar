from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.state.models import (
    ActionItem,
    ActionItemStatus,
    Event,
    EventCaregiver,
    EventChild,
    RsvpStatus,
)


async def create_event(session: AsyncSession, family_id: UUID, all_day: bool = False, time_tbd: bool = False, time_explicit: bool = False, **kwargs) -> Event:
    event = Event(family_id=family_id, all_day=all_day, time_tbd=time_tbd, time_explicit=time_explicit, **kwargs)
    session.add(event)
    await session.flush()
    return event


async def get_event(session: AsyncSession, family_id: UUID, event_id: UUID) -> Event | None:
    result = await session.execute(
        select(Event).where(Event.family_id == family_id, Event.id == event_id)
        .options(selectinload(Event.children), selectinload(Event.caregivers))
    )
    return result.scalar_one_or_none()


async def get_events_in_range(
    session: AsyncSession,
    family_id: UUID,
    start: datetime,
    end: datetime,
    confirmed_only: bool = True,
) -> list[Event]:
    filters = [
        Event.family_id == family_id,
        Event.cancelled_at.is_(None),
        Event.datetime_start >= start,
        Event.datetime_start < end,
    ]
    if confirmed_only:
        filters.append(Event.confirmed_by_caregiver.is_(True))

    result = await session.execute(
        select(Event)
        .where(*filters)
        .options(selectinload(Event.children), selectinload(Event.caregivers))
        .order_by(Event.datetime_start)
    )
    return list(result.scalars().all())


async def get_upcoming_events(
    session: AsyncSession, family_id: UUID, days: int = 7,
    family_timezone: str | None = None,
    confirmed_only: bool = True,
) -> list[Event]:
    if family_timezone:
        from src.utils.timezone import get_family_now
        now = get_family_now(family_timezone)
    else:
        now = datetime.now(UTC)
    end = now + timedelta(days=days)
    return await get_events_in_range(session, family_id, now, end, confirmed_only=confirmed_only)


async def get_unconfirmed_events(
    session: AsyncSession, family_id: UUID, future_only: bool = True
) -> list[Event]:
    """Get unconfirmed events that haven't been cancelled.

    If future_only=True (default), only returns events whose start time is in the future.
    Used for daily digest resurfacing and cleanup of past unconfirmed events.
    """
    filters = [
        Event.family_id == family_id,
        Event.cancelled_at.is_(None),
        Event.confirmed_by_caregiver.is_(False),
    ]
    if future_only:
        filters.append(Event.datetime_start > datetime.now(UTC))

    result = await session.execute(
        select(Event)
        .where(*filters)
        .options(selectinload(Event.children), selectinload(Event.caregivers))
        .order_by(Event.datetime_start)
    )
    return list(result.scalars().all())


async def get_events_needing_rsvp(
    session: AsyncSession, family_id: UUID
) -> list[Event]:
    result = await session.execute(
        select(Event).where(
            Event.family_id == family_id,
            Event.cancelled_at.is_(None),
            Event.rsvp_status == RsvpStatus.pending,
            Event.rsvp_deadline.is_not(None),
        ).options(selectinload(Event.children), selectinload(Event.caregivers))
        .order_by(Event.rsvp_deadline)
    )
    return list(result.scalars().all())


async def find_duplicate_event(
    session: AsyncSession,
    family_id: UUID,
    title: str,
    datetime_start: datetime,
    threshold_minutes: int = 30,
    title_similarity_threshold: float = 0.7,
    child_ids: list[UUID] | None = None,
    all_day: bool = False,
) -> Event | None:
    """Find a potential duplicate event based on datetime proximity and title similarity.

    Uses ±threshold_minutes for datetime and token overlap for title similarity.
    If child_ids is provided, only matches events with the same set of children
    (prevents merging "Emma's soccer" with "Jake's soccer" at the same time).

    Also queries all-day events on the same calendar date so that an all-day
    placeholder and a timed event for the same day are recognised as duplicates.
    """
    window_start = datetime_start - timedelta(minutes=threshold_minutes)
    window_end = datetime_start + timedelta(minutes=threshold_minutes)

    # Query 1: timed-event ±threshold window (existing behaviour)
    result = await session.execute(
        select(Event).where(
            Event.family_id == family_id,
            Event.cancelled_at.is_(None),
            Event.datetime_start >= window_start,
            Event.datetime_start <= window_end,
        ).options(selectinload(Event.children), selectinload(Event.caregivers))
    )
    candidates = list(result.scalars().all())

    # Query 2: all-day events on the same calendar date
    target_date = datetime_start.date()
    result_allday = await session.execute(
        select(Event).where(
            Event.family_id == family_id,
            Event.cancelled_at.is_(None),
            (Event.all_day.is_(True) | Event.time_tbd.is_(True)),
            func.date(Event.datetime_start) == target_date,
        ).options(selectinload(Event.children), selectinload(Event.caregivers))
    )
    allday_candidates = list(result_allday.scalars().all())

    # Merge candidates, avoiding duplicates
    seen_ids = {c.id for c in candidates}
    for c in allday_candidates:
        if c.id not in seen_ids:
            candidates.append(c)
            seen_ids.add(c.id)

    for candidate in candidates:
        similarity = compute_title_similarity(title, candidate.title)
        if similarity >= title_similarity_threshold:
            # If child_ids provided, only match if children overlap
            if child_ids is not None:
                candidate_child_ids = {ec.child_id for ec in candidate.children}
                if set(child_ids) != candidate_child_ids:
                    continue
            return candidate

    return None


def compute_title_similarity(a: str, b: str) -> float:
    """Compute title similarity using token overlap (Jaccard-like).

    Normalizes, tokenizes, and computes overlap ratio.
    """
    tokens_a = _tokenize(a)
    tokens_b = _tokenize(b)

    if not tokens_a and not tokens_b:
        return 1.0
    if not tokens_a or not tokens_b:
        return 0.0

    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(intersection) / len(union)


def _tokenize(text: str) -> set[str]:
    """Normalize and tokenize a title for comparison."""
    import re

    text = text.lower().strip()
    # Remove common noise words
    text = re.sub(r"[^\w\s]", " ", text)
    tokens = set(text.split())
    # Remove very short tokens
    tokens = {t for t in tokens if len(t) > 1}
    return tokens


async def update_event(
    session: AsyncSession, family_id: UUID, event_id: UUID, **kwargs
) -> Event:
    event = await get_event(session, family_id, event_id)
    if event is None:
        raise ValueError(f"Event {event_id} not found for family {family_id}")
    for key, value in kwargs.items():
        setattr(event, key, value)
    await session.flush()
    return event


async def link_children_to_event(
    session: AsyncSession, family_id: UUID, event_id: UUID, child_ids: list[UUID]
) -> None:
    for child_id in child_ids:
        link = EventChild(event_id=event_id, child_id=child_id, family_id=family_id)
        session.add(link)
    await session.flush()


async def link_caregivers_to_event(
    session: AsyncSession, family_id: UUID, event_id: UUID, caregiver_ids: list[UUID]
) -> None:
    for caregiver_id in caregiver_ids:
        link = EventCaregiver(event_id=event_id, caregiver_id=caregiver_id, family_id=family_id)
        session.add(link)
    await session.flush()


async def replace_children_on_event(
    session: AsyncSession, family_id: UUID, event_id: UUID, child_ids: list[UUID]
) -> None:
    """Delete existing child links and re-link with the given child_ids."""
    from sqlalchemy import delete

    await session.execute(
        delete(EventChild).where(
            EventChild.event_id == event_id,
            EventChild.family_id == family_id,
        )
    )
    for child_id in child_ids:
        session.add(EventChild(event_id=event_id, child_id=child_id, family_id=family_id))
    await session.flush()


async def replace_caregivers_on_event(
    session: AsyncSession, family_id: UUID, event_id: UUID, caregiver_ids: list[UUID]
) -> None:
    """Delete existing caregiver links and re-link with the given caregiver_ids."""
    from sqlalchemy import delete

    await session.execute(
        delete(EventCaregiver).where(
            EventCaregiver.event_id == event_id,
            EventCaregiver.family_id == family_id,
        )
    )
    for caregiver_id in caregiver_ids:
        session.add(EventCaregiver(event_id=event_id, caregiver_id=caregiver_id, family_id=family_id))
    await session.flush()


async def get_events_by_source_ref(
    session: AsyncSession, family_id: UUID, source_ref: str,
    include_cancelled: bool = False,
) -> list[Event]:
    filters = [
        Event.family_id == family_id,
        Event.source_refs.any(source_ref),
    ]
    if not include_cancelled:
        filters.append(Event.cancelled_at.is_(None))
    result = await session.execute(
        select(Event).where(*filters)
        .options(selectinload(Event.children), selectinload(Event.caregivers))
    )
    return list(result.scalars().all())


# ── Action items ────────────────────────────────────────────────────────


async def create_action_item(session: AsyncSession, family_id: UUID, **kwargs) -> ActionItem:
    item = ActionItem(family_id=family_id, **kwargs)
    session.add(item)
    await session.flush()
    return item


async def get_pending_action_items(
    session: AsyncSession, family_id: UUID
) -> list[ActionItem]:
    result = await session.execute(
        select(ActionItem).where(
            ActionItem.family_id == family_id,
            ActionItem.status == ActionItemStatus.pending,
        ).order_by(ActionItem.due_date.asc().nullslast())
    )
    return list(result.scalars().all())


async def get_action_items_due_soon(
    session: AsyncSession, family_id: UUID, within_hours: int = 48,
    family_timezone: str | None = None,
) -> list[ActionItem]:
    if family_timezone:
        from src.utils.timezone import get_family_now
        now = get_family_now(family_timezone)
    else:
        now = datetime.now(UTC)
    cutoff = now + timedelta(hours=within_hours)
    result = await session.execute(
        select(ActionItem).where(
            ActionItem.family_id == family_id,
            ActionItem.status == ActionItemStatus.pending,
            ActionItem.due_date.is_not(None),
            ActionItem.due_date <= cutoff,
        ).order_by(ActionItem.due_date)
    )
    return list(result.scalars().all())
