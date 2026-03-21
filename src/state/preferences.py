"""DAL for caregiver_preferences — structured preferences that gate system behavior."""

from datetime import time
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.state.models import CaregiverPreferences


async def get_or_create_preferences(
    session: AsyncSession,
    caregiver_id: UUID,
    family_id: UUID,
) -> CaregiverPreferences:
    """Get existing preferences or create default row for a caregiver."""
    result = await session.execute(
        select(CaregiverPreferences).where(
            CaregiverPreferences.caregiver_id == caregiver_id,
        )
    )
    prefs = result.scalar_one_or_none()

    if prefs is None:
        prefs = CaregiverPreferences(
            caregiver_id=caregiver_id,
            family_id=family_id,
        )
        session.add(prefs)
        await session.flush()

    return prefs


async def update_preference(
    session: AsyncSession,
    caregiver_id: UUID,
    family_id: UUID,
    **fields,
) -> CaregiverPreferences:
    """Update structured preference fields for a caregiver.

    Accepts any combination of: quiet_hours_start, quiet_hours_end, delegation_areas.
    """
    prefs = await get_or_create_preferences(session, caregiver_id, family_id)

    for key, value in fields.items():
        if hasattr(prefs, key):
            setattr(prefs, key, value)

    await session.flush()
    return prefs


async def get_quiet_hours(
    session: AsyncSession, caregiver_id: UUID
) -> tuple[time, time] | None:
    """Returns (start, end) quiet hours or None if not set."""
    result = await session.execute(
        select(
            CaregiverPreferences.quiet_hours_start,
            CaregiverPreferences.quiet_hours_end,
        ).where(CaregiverPreferences.caregiver_id == caregiver_id)
    )
    row = result.one_or_none()
    if row and row[0] is not None and row[1] is not None:
        return (row[0], row[1])
    return None


async def is_in_quiet_hours(
    session: AsyncSession, caregiver_id: UUID, current_time: time
) -> bool:
    """Check if the given time falls within the caregiver's quiet hours."""
    hours = await get_quiet_hours(session, caregiver_id)
    if hours is None:
        return False

    start, end = hours
    if start <= end:
        # Same-day range (e.g., 22:00 - 23:00) — not typical but handle it
        return start <= current_time <= end
    else:
        # Overnight range (e.g., 22:00 - 07:00)
        return current_time >= start or current_time <= end
