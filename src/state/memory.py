from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.state.models import ConversationMemory


async def store_message(
    session: AsyncSession,
    family_id: UUID,
    content: str,
    msg_type: str = "short_term",
    expires_at: datetime | None = None,
) -> ConversationMemory:
    memory = ConversationMemory(
        family_id=family_id,
        type=msg_type,
        content=content,
        expires_at=expires_at,
    )
    session.add(memory)
    await session.flush()
    return memory


async def get_recent_messages(
    session: AsyncSession, family_id: UUID, limit: int = 20
) -> list[ConversationMemory]:
    result = await session.execute(
        select(ConversationMemory)
        .where(ConversationMemory.family_id == family_id)
        .order_by(ConversationMemory.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def cleanup_expired(session: AsyncSession, family_id: UUID) -> int:
    now = datetime.now().astimezone()
    result = await session.execute(
        delete(ConversationMemory).where(
            ConversationMemory.family_id == family_id,
            ConversationMemory.expires_at.is_not(None),
            ConversationMemory.expires_at < now,
        )
    )
    await session.flush()
    return result.rowcount
