from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.state.models import Child


async def create_child(
    session: AsyncSession,
    family_id: UUID,
    name: str,
    date_of_birth: date | None = None,
) -> Child:
    child = Child(family_id=family_id, name=name, date_of_birth=date_of_birth)
    session.add(child)
    await session.flush()
    return child


async def get_children_for_family(
    session: AsyncSession, family_id: UUID
) -> list[Child]:
    result = await session.execute(
        select(Child).where(Child.family_id == family_id).order_by(Child.name)
    )
    return list(result.scalars().all())


async def get_child(session: AsyncSession, family_id: UUID, child_id: UUID) -> Child | None:
    result = await session.execute(
        select(Child).where(Child.family_id == family_id, Child.id == child_id)
    )
    return result.scalar_one_or_none()


async def fuzzy_match_child(
    session: AsyncSession, family_id: UUID, name_text: str
) -> Child | None:
    """Fuzzy match a child name against the family's children.

    Handles exact matches, case-insensitive matches, partial matches,
    and common nickname patterns.
    """
    children = await get_children_for_family(session, family_id)
    if not children:
        return None

    name_lower = name_text.lower().strip()

    # Exact case-insensitive match
    for child in children:
        if child.name.lower() == name_lower:
            return child

    # Prefix/contains match
    for child in children:
        child_lower = child.name.lower()
        if name_lower.startswith(child_lower) or child_lower.startswith(name_lower):
            return child

    # Token overlap: any word in name matches any word in child name
    name_tokens = set(name_lower.split())
    for child in children:
        child_tokens = set(child.name.lower().split())
        if name_tokens & child_tokens:
            return child

    return None
