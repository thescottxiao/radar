from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.state.models import Caregiver, Family


async def create_family(
    session: AsyncSession,
    whatsapp_group_id: str,
    timezone_str: str = "America/New_York",
    forward_email: str | None = None,
) -> Family:
    family = Family(
        whatsapp_group_id=whatsapp_group_id,
        timezone=timezone_str,
        forward_email=forward_email or "",
    )
    session.add(family)
    await session.flush()
    # Generate forward email from ID if not provided
    if not forward_email:
        family.forward_email = f"family-{family.id}@radar.app"
        await session.flush()
    return family


async def get_family(session: AsyncSession, family_id: UUID) -> Family | None:
    return await session.get(Family, family_id)


async def get_family_by_group_id(
    session: AsyncSession, whatsapp_group_id: str
) -> Family | None:
    result = await session.execute(
        select(Family).where(Family.whatsapp_group_id == whatsapp_group_id)
    )
    return result.scalar_one_or_none()


async def get_family_by_forward_email(
    session: AsyncSession, email: str
) -> Family | None:
    result = await session.execute(
        select(Family).where(Family.forward_email == email)
    )
    return result.scalar_one_or_none()


async def create_caregiver(
    session: AsyncSession,
    family_id: UUID,
    whatsapp_phone: str,
    name: str | None = None,
) -> Caregiver:
    caregiver = Caregiver(
        family_id=family_id,
        whatsapp_phone=whatsapp_phone,
        name=name,
    )
    session.add(caregiver)
    await session.flush()
    return caregiver


async def get_caregiver_by_phone(
    session: AsyncSession, family_id: UUID, phone: str
) -> Caregiver | None:
    result = await session.execute(
        select(Caregiver).where(
            Caregiver.family_id == family_id,
            Caregiver.whatsapp_phone == phone,
        )
    )
    return result.scalar_one_or_none()


async def find_caregiver_by_phone(
    session: AsyncSession, phone: str
) -> Caregiver | None:
    """Find a caregiver by phone across all families (for DM lookup)."""
    result = await session.execute(
        select(Caregiver).where(
            Caregiver.whatsapp_phone == phone,
            Caregiver.is_active.is_(True),
        )
    )
    return result.scalar_one_or_none()


async def get_caregiver_by_email(
    session: AsyncSession, email: str
) -> Caregiver | None:
    result = await session.execute(
        select(Caregiver).where(Caregiver.google_account_email == email)
    )
    return result.scalar_one_or_none()


async def get_caregivers_for_family(
    session: AsyncSession, family_id: UUID
) -> list[Caregiver]:
    result = await session.execute(
        select(Caregiver).where(
            Caregiver.family_id == family_id,
            Caregiver.is_active.is_(True),
        )
    )
    return list(result.scalars().all())


async def update_caregiver_google_tokens(
    session: AsyncSession,
    caregiver_id: UUID,
    email: str,
    refresh_token_encrypted: bytes,
    token_expires_at: datetime,
) -> Caregiver:
    caregiver = await session.get(Caregiver, caregiver_id)
    if caregiver is None:
        raise ValueError(f"Caregiver {caregiver_id} not found")
    caregiver.google_account_email = email
    caregiver.google_refresh_token_encrypted = refresh_token_encrypted
    caregiver.google_token_expires_at = token_expires_at
    await session.flush()
    return caregiver


async def update_family_timezone(
    session: AsyncSession, family_id: UUID, timezone: str
) -> Family:
    """Update the family's timezone (e.g., inferred from Google Calendar settings)."""
    family = await session.get(Family, family_id)
    if family is None:
        raise ValueError(f"Family {family_id} not found")
    family.timezone = timezone
    await session.flush()
    return family


async def get_caregivers_needing_watch_renewal(
    session: AsyncSession, within_hours: int = 48
) -> list[Caregiver]:
    """Get caregivers whose Gmail or GCal watches expire within the given hours."""
    cutoff = datetime.now(UTC) + timedelta(hours=within_hours)
    result = await session.execute(
        select(Caregiver).where(
            Caregiver.is_active.is_(True),
            Caregiver.google_refresh_token_encrypted.is_not(None),
            (
                (Caregiver.gmail_watch_expiry < cutoff)
                | (Caregiver.gcal_watch_expiry < cutoff)
                | (Caregiver.gmail_watch_expiry.is_(None))
                | (Caregiver.gcal_watch_expiry.is_(None))
            ),
        )
    )
    return list(result.scalars().all())


async def get_families_with_google(session: AsyncSession) -> list[Family]:
    """Get all families that have at least one caregiver with Google tokens."""
    result = await session.execute(
        select(Family).where(
            exists(
                select(Caregiver.id).where(
                    Caregiver.family_id == Family.id,
                    Caregiver.is_active.is_(True),
                    Caregiver.google_refresh_token_encrypted.is_not(None),
                )
            )
        )
    )
    return list(result.scalars().all())
