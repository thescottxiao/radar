import asyncio
import os
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from src.state.models import (
    Base,
    Caregiver,
    Child,
    Event,
    EventSource,

    Family,
    RsvpMethod,
    RsvpStatus,
)

# Test database URL — uses the db-test service from docker-compose
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://radar_test:radar_test@localhost:5433/radar_test",
)

# Check if PostgreSQL is available
_pg_available = None


def pg_available() -> bool:
    global _pg_available
    if _pg_available is None:
        import asyncio

        async def _check():
            try:
                eng = create_async_engine(TEST_DATABASE_URL)
                async with eng.connect() as conn:
                    await conn.execute(text("SELECT 1"))
                await eng.dispose()
                return True
            except Exception:
                return False

        _pg_available = asyncio.get_event_loop().run_until_complete(_check()) if asyncio.get_event_loop().is_running() is False else False
    return _pg_available


requires_db = pytest.mark.skipif(
    os.environ.get("SKIP_DB_TESTS", "1") == "1",
    reason="Database not available (set SKIP_DB_TESTS=0 to run)",
)


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
async def engine():
    eng = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with eng.begin() as conn:
        await conn.execute(text('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"'))
        await conn.execute(text('CREATE EXTENSION IF NOT EXISTS "vector"'))
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await eng.dispose()


@pytest.fixture
async def session(engine) -> AsyncSession:
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        async with session.begin():
            yield session
            await session.rollback()


@pytest.fixture
async def sample_family(session: AsyncSession) -> dict:
    """Create a sample family with 2 caregivers and 2 children."""
    family = Family(
        whatsapp_group_id=f"test-group-{id(session)}",
        forward_email=f"family-test-{id(session)}@radar.app",
        timezone="America/New_York",
        onboarding_complete=True,
    )
    session.add(family)
    await session.flush()

    sarah = Caregiver(
        family_id=family.id,
        whatsapp_phone="+15551234567",
        name="Sarah",
        google_account_email="sarah@gmail.com",
    )
    mike = Caregiver(
        family_id=family.id,
        whatsapp_phone="+15559876543",
        name="Mike",
        google_account_email="mike@gmail.com",
    )
    session.add_all([sarah, mike])
    await session.flush()

    emma = Child(
        family_id=family.id,
        name="Emma",
        date_of_birth=date(2017, 5, 15),
        activities=["soccer", "piano"],
    )
    jake = Child(
        family_id=family.id,
        name="Jake",
        date_of_birth=date(2019, 8, 22),
        activities=["swim"],
    )
    session.add_all([emma, jake])
    await session.flush()

    return {
        "family": family,
        "sarah": sarah,
        "mike": mike,
        "emma": emma,
        "jake": jake,
    }


@pytest.fixture
async def sample_events(session: AsyncSession, sample_family: dict) -> list[Event]:
    """Create sample events for testing."""
    family = sample_family["family"]
    now = datetime.now(UTC)

    events = [
        Event(
            family_id=family.id,
            source=EventSource.manual,
            type="sports_practice",
            title="Soccer Practice",
            datetime_start=now + timedelta(days=1, hours=2),
            datetime_end=now + timedelta(days=1, hours=3, minutes=30),
            location="Westfield Fields",
        ),
        Event(
            family_id=family.id,
            source=EventSource.calendar,
            type="birthday_party",
            title="Sophia's Birthday Party",
            datetime_start=now + timedelta(days=3, hours=5),
            datetime_end=now + timedelta(days=3, hours=7),
            location="JumpZone",
            rsvp_status=RsvpStatus.pending,
            rsvp_deadline=now + timedelta(days=2),
            rsvp_method=RsvpMethod.reply_email,
            rsvp_contact="sophia.mom@email.com",
        ),
        Event(
            family_id=family.id,
            source=EventSource.manual,
            type="school_event",
            title="Parent Teacher Conference",
            datetime_start=now + timedelta(days=5, hours=4),
            datetime_end=now + timedelta(days=5, hours=4, minutes=30),
            location="Lincoln Elementary",
        ),
    ]
    session.add_all(events)
    await session.flush()
    return events
