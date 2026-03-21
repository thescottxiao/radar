from uuid import UUID

from sqlalchemy import and_, select, update
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
    caregiver_id: UUID | None = None,
    confirmed: bool = False,
) -> FamilyLearning:
    learning = FamilyLearning(
        family_id=family_id,
        caregiver_id=caregiver_id,
        category=category,
        fact=fact,
        source=source,
        confidence=confidence,
        entity_type=entity_type,
        entity_id=entity_id,
        confirmed=confirmed,
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
            FamilyLearning.graduated.is_(False),
            FamilyLearning.superseded_by.is_(None),
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


async def get_confirmed_learnings(
    session: AsyncSession, family_id: UUID
) -> list[FamilyLearning]:
    """Get all confirmed, non-graduated, non-superseded learnings for prompt context."""
    result = await session.execute(
        select(FamilyLearning).where(
            FamilyLearning.family_id == family_id,
            FamilyLearning.confirmed.is_(True),
            FamilyLearning.graduated.is_(False),
            FamilyLearning.superseded_by.is_(None),
        ).order_by(FamilyLearning.category, FamilyLearning.created_at.desc())
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


_PREF_CATEGORIES = {
    "pref_communication", "pref_scheduling", "pref_notification",
    "pref_prep", "pref_delegation", "pref_decision",
}


async def get_active_preferences(
    session: AsyncSession,
    family_id: UUID,
    caregiver_id: UUID | None = None,
) -> list[FamilyLearning]:
    """Get confirmed pref_* learnings, merging family-wide + caregiver-specific.

    Family-wide prefs have caregiver_id=NULL. If caregiver_id is provided,
    also includes that caregiver's personal preferences.
    """
    conditions = [
        FamilyLearning.family_id == family_id,
        FamilyLearning.confirmed.is_(True),
        FamilyLearning.graduated.is_(False),
        FamilyLearning.superseded_by.is_(None),
        FamilyLearning.category.in_(_PREF_CATEGORIES),
    ]

    if caregiver_id:
        # Family-wide (NULL) + this caregiver's personal prefs
        conditions.append(
            FamilyLearning.caregiver_id.in_([caregiver_id])
            | FamilyLearning.caregiver_id.is_(None)
        )
    else:
        # Family-wide only
        conditions.append(FamilyLearning.caregiver_id.is_(None))

    result = await session.execute(
        select(FamilyLearning).where(
            and_(*conditions)
        ).order_by(FamilyLearning.category, FamilyLearning.created_at.desc())
    )
    return list(result.scalars().all())


async def supersede_learning(
    session: AsyncSession,
    old_learning_id: UUID,
    family_id: UUID,
    new_fact: str,
    source: str | None = None,
) -> FamilyLearning:
    """Supersede an existing learning with a corrected version.

    The old learning's superseded_by points to the new one.
    The new learning is created as confirmed.
    """
    # Fetch the old learning to copy its fields
    old = await session.get(FamilyLearning, old_learning_id)
    if not old or old.family_id != family_id:
        raise ValueError(f"Learning {old_learning_id} not found for family {family_id}")

    new_learning = FamilyLearning(
        family_id=family_id,
        caregiver_id=old.caregiver_id,
        category=old.category,
        entity_type=old.entity_type,
        entity_id=old.entity_id,
        fact=new_fact,
        source=source or f"Correction of previous learning",
        confidence=1.0,
        confirmed=True,
    )
    session.add(new_learning)
    await session.flush()

    # Point old learning to new one
    old.superseded_by = new_learning.id
    await session.flush()

    return new_learning


async def graduate_learning(
    session: AsyncSession, learning_id: UUID, family_id: UUID
) -> None:
    """Mark a learning as graduated after promoting to a structured table."""
    await session.execute(
        update(FamilyLearning)
        .where(
            FamilyLearning.id == learning_id,
            FamilyLearning.family_id == family_id,
        )
        .values(graduated=True)
    )
    await session.flush()


async def auto_confirm_previously_surfaced(
    session: AsyncSession, family_id: UUID
) -> list[UUID]:
    """Confirm learnings that were surfaced in a previous summary and not corrected.

    Returns list of newly confirmed learning IDs.
    """
    result = await session.execute(
        select(FamilyLearning.id).where(
            FamilyLearning.family_id == family_id,
            FamilyLearning.surfaced_in_summary.is_(True),
            FamilyLearning.confirmed.is_(False),
            FamilyLearning.graduated.is_(False),
            FamilyLearning.superseded_by.is_(None),
        )
    )
    ids = list(result.scalars().all())

    if ids:
        await session.execute(
            update(FamilyLearning)
            .where(
                FamilyLearning.family_id == family_id,
                FamilyLearning.id.in_(ids),
            )
            .values(confirmed=True)
        )
        await session.flush()

    return ids
