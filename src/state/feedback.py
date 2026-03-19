from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.state.models import ExtractionFeedback


async def log_correction(
    session: AsyncSession,
    family_id: UUID,
    raw_email_hash: str,
    original_extraction: dict,
    corrected_extraction: dict,
    correction_type: str,
) -> ExtractionFeedback:
    feedback = ExtractionFeedback(
        family_id=family_id,
        raw_email_hash=raw_email_hash,
        original_extraction=original_extraction,
        corrected_extraction=corrected_extraction,
        correction_type=correction_type,
    )
    session.add(feedback)
    await session.flush()
    return feedback


async def get_corrections(
    session: AsyncSession, family_id: UUID, limit: int = 100
) -> list[ExtractionFeedback]:
    result = await session.execute(
        select(ExtractionFeedback)
        .where(ExtractionFeedback.family_id == family_id)
        .order_by(ExtractionFeedback.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())
