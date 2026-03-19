"""Tenant lifecycle: family creation and onboarding."""

import logging
from datetime import date
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.state import children as children_dal
from src.state import families as families_dal
from src.state.models import Child, Family

logger = logging.getLogger(__name__)


async def create_tenant(
    session: AsyncSession,
    whatsapp_group_id: str,
    timezone: str = "America/New_York",
) -> Family:
    """Create a new family tenant with a generated forward email.

    The forward email is generated as family-{id}@{forward_email_domain}.
    """
    family = await families_dal.create_family(
        session,
        whatsapp_group_id=whatsapp_group_id,
        timezone_str=timezone,
    )
    # Ensure forward email uses configured domain
    family.forward_email = f"family-{family.id}@{settings.forward_email_domain}"
    await session.flush()

    logger.info(
        "Created tenant: family_id=%s, group=%s, forward_email=%s",
        family.id,
        whatsapp_group_id,
        family.forward_email,
    )
    return family


async def onboard_family(
    session: AsyncSession,
    family_id: UUID,
    children_info: list[dict],
) -> list[Child]:
    """Create child records from onboarding data and mark family as onboarded.

    Each dict in children_info should have:
      - name (required)
      - date_of_birth (optional, ISO date string or date object)
      - age (optional, int — used to estimate date_of_birth if not provided)
    """
    family = await families_dal.get_family(session, family_id)
    if family is None:
        raise ValueError(f"Family {family_id} not found")

    created_children: list[Child] = []
    for info in children_info:
        name = info.get("name")
        if not name:
            logger.warning("Skipping child with no name in onboarding data")
            continue

        dob = info.get("date_of_birth")
        if isinstance(dob, str):
            dob = date.fromisoformat(dob)
        elif dob is None and "age" in info:
            # Estimate date_of_birth from age
            age = int(info["age"])
            today = date.today()
            dob = today.replace(year=today.year - age)

        child = await children_dal.create_child(
            session,
            family_id=family_id,
            name=name,
            date_of_birth=dob,
        )
        created_children.append(child)

    # Mark onboarding complete
    family.onboarding_complete = True
    await session.flush()

    logger.info(
        "Onboarded family %s with %d children: %s",
        family_id,
        len(created_children),
        [c.name for c in created_children],
    )
    return created_children
