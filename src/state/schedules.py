"""Data access layer for RecurringSchedule records."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.state.models import RecurringSchedule


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
