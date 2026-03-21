from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.state.models import FamilyLearning


async def create_learning(
    session: AsyncSession,
    family_id: UUID,
    category: str,
    fact: str,
    source: str | None = None,
    confidence: float = 0.5,
    entity_type: str | None = None,
    entity_id: UUID | None = None,
) -> FamilyLearning:
    learning = FamilyLearning(
        family_id=family_id,
        category=category,
        fact=fact,
        source=source,
        confidence=confidence,
        entity_type=entity_type,
        entity_id=entity_id,
    )
    session.add(learning)
    await session.flush()
    return learning


async def get_unsurfaced_learnings(
    session: AsyncSession, family_id: UUID
) -> list[FamilyLearning]:
    result = await session.execute(
        select(FamilyLearning).where(
            FamilyLearning.family_id == family_id,
            FamilyLearning.surfaced_in_summary.is_(False),
        ).order_by(FamilyLearning.created_at.desc())
    )
    return list(result.scalars().all())


async def mark_surfaced(
    session: AsyncSession, family_id: UUID, learning_ids: list[UUID]
) -> None:
    if not learning_ids:
        return
    await session.execute(
        update(FamilyLearning)
        .where(
            FamilyLearning.family_id == family_id,
            FamilyLearning.id.in_(learning_ids),
        )
        .values(surfaced_in_summary=True)
    )
    await session.flush()


async def confirm_learnings(
    session: AsyncSession, family_id: UUID, learning_ids: list[UUID]
) -> None:
    if not learning_ids:
        return
    await session.execute(
        update(FamilyLearning)
        .where(
            FamilyLearning.family_id == family_id,
            FamilyLearning.id.in_(learning_ids),
        )
        .values(confirmed=True)
    )
    await session.flush()


async def get_learnings_by_category(
    session: AsyncSession, family_id: UUID, category: str
) -> list[FamilyLearning]:
    result = await session.execute(
        select(FamilyLearning).where(
            FamilyLearning.family_id == family_id,
            FamilyLearning.category == category,
        ).order_by(FamilyLearning.created_at.desc())
    )
    return list(result.scalars().all())


async def get_learning_by_source(
    session: AsyncSession,
    family_id: UUID,
    category: str,
    entity_id: UUID,
    source: str,
) -> FamilyLearning | None:
    """Find a specific learning entry by category, entity_id, and exact source match."""
    result = await session.execute(
        select(FamilyLearning).where(
            FamilyLearning.family_id == family_id,
            FamilyLearning.category == category,
            FamilyLearning.entity_id == entity_id,
            FamilyLearning.source == source,
        )
    )
    return result.scalar_one_or_none()
