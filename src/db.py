from collections.abc import AsyncGenerator
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.config import settings

engine = create_async_engine(settings.database_url, echo=False, pool_size=10, max_overflow=20)
async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_factory() as session:
        yield session


async def set_tenant(session: AsyncSession, family_id: UUID) -> None:
    """Set the current tenant for row-level security policies."""
    await session.execute(
        __import__("sqlalchemy").text("SET app.current_family_id = :fid"),
        {"fid": str(family_id)},
    )
