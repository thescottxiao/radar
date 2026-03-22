"""Data access layer for RecurringSchedule and ScheduleException records."""

from datetime import date
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.state.events import compute_title_similarity
from src.state.models import RecurringSchedule, ScheduleException


async def get_recurring_schedule(
    session: AsyncSession, family_id: UUID, schedule_id: UUID
) -> RecurringSchedule | None:
    result = await session.execute(
        select(RecurringSchedule).where(
            RecurringSchedule.family_id == family_id,
            RecurringSchedule.id == schedule_id,
        )
    )
    return result.scalar_one_or_none()


async def get_schedules_for_family(
    session: AsyncSession, family_id: UUID
) -> list[RecurringSchedule]:
    result = await session.execute(
        select(RecurringSchedule)
        .where(RecurringSchedule.family_id == family_id)
        .order_by(RecurringSchedule.activity_name)
    )
    return list(result.scalars().all())


async def update_schedule_defaults(
    session: AsyncSession,
    family_id: UUID,
    schedule_id: UUID,
    **kwargs,
) -> RecurringSchedule:
    """Update transport defaults on a RecurringSchedule.

    Typical kwargs: default_drop_off_caregiver, default_pick_up_caregiver.
    """
    schedule = await get_recurring_schedule(session, family_id, schedule_id)
    if schedule is None:
        raise ValueError(
            f"RecurringSchedule {schedule_id} not found for family {family_id}"
        )
    for key, value in kwargs.items():
        setattr(schedule, key, value)
    await session.flush()
    return schedule


async def create_recurring_schedule(
    session: AsyncSession,
    family_id: UUID,
    *,
    activity_name: str,
    pattern: str,
    rrule: str,
    start_date: date,
    end_date: date | None = None,
    child_id: UUID | None = None,
    activity_type: str = "other",
    location: str | None = None,
    confirmed: bool = False,
) -> RecurringSchedule:
    """Create a new recurring schedule."""
    schedule = RecurringSchedule(
        family_id=family_id,
        child_id=child_id,
        activity_name=activity_name,
        activity_type=activity_type,
        pattern=pattern,
        rrule=rrule,
        location=location,
        start_date=start_date,
        end_date=end_date,
        confirmed=confirmed,
    )
    session.add(schedule)
    await session.flush()
    return schedule


async def delete_recurring_schedule(
    session: AsyncSession, family_id: UUID, schedule_id: UUID
) -> None:
    """Delete a recurring schedule and its exceptions (cascade)."""
    schedule = await get_recurring_schedule(session, family_id, schedule_id)
    if schedule:
        await session.delete(schedule)
        await session.flush()


async def get_active_schedules_for_family(
    session: AsyncSession, family_id: UUID,
    family_timezone: str | None = None,
) -> list[RecurringSchedule]:
    """Get schedules that are currently active (end_date is NULL or >= today)."""
    if family_timezone:
        from src.utils.timezone import get_family_today
        today = get_family_today(family_timezone)
    else:
        today = date.today()
    result = await session.execute(
        select(RecurringSchedule)
        .where(
            RecurringSchedule.family_id == family_id,
            or_(
                RecurringSchedule.end_date.is_(None),
                RecurringSchedule.end_date >= today,
            ),
        )
        .order_by(RecurringSchedule.activity_name)
    )
    return list(result.scalars().all())


async def find_similar_schedule(
    session: AsyncSession, family_id: UUID, activity_name: str, threshold: float = 0.7
) -> RecurringSchedule | None:
    """Find an existing schedule with a similar activity name (for dedup)."""
    schedules = await get_active_schedules_for_family(session, family_id)
    for schedule in schedules:
        if compute_title_similarity(activity_name, schedule.activity_name) >= threshold:
            return schedule
    return None


async def create_schedule_exception(
    session: AsyncSession,
    family_id: UUID,
    schedule_id: UUID,
    original_date: date,
    exception_type: str,
    *,
    new_date: date | None = None,
    new_location: str | None = None,
    reason: str | None = None,
) -> ScheduleException:
    """Create an exception to a recurring schedule (cancel, reschedule, etc.)."""
    exception = ScheduleException(
        recurring_schedule_id=schedule_id,
        family_id=family_id,
        original_date=original_date,
        exception_type=exception_type,
        new_date=new_date,
        new_location=new_location,
        reason=reason,
    )
    session.add(exception)
    await session.flush()
    return exception
