import os

import pytest
from src.state import families

pytestmark = pytest.mark.skipif(
    os.environ.get("SKIP_DB_TESTS", "1") == "1", reason="Database not available"
)


class TestFamilies:
    async def test_create_family(self, session):
        family = await families.create_family(session, "group-1")
        assert family.id is not None
        assert family.whatsapp_group_id == "group-1"
        assert family.forward_email == f"family-{family.id}@radar.app"
        assert family.onboarding_complete is False

    async def test_get_family_by_group_id(self, session):
        family = await families.create_family(session, "group-lookup")
        found = await families.get_family_by_group_id(session, "group-lookup")
        assert found is not None
        assert found.id == family.id

    async def test_get_family_not_found(self, session):
        found = await families.get_family_by_group_id(session, "nonexistent")
        assert found is None

    async def test_create_caregiver(self, session):
        family = await families.create_family(session, "group-cg")
        cg = await families.create_caregiver(session, family.id, "+15551111111", "Alice")
        assert cg.id is not None
        assert cg.name == "Alice"
        assert cg.family_id == family.id

    async def test_get_caregiver_by_phone(self, session):
        family = await families.create_family(session, "group-phone")
        await families.create_caregiver(session, family.id, "+15552222222", "Bob")
        found = await families.get_caregiver_by_phone(session, family.id, "+15552222222")
        assert found is not None
        assert found.name == "Bob"

    async def test_get_caregivers_for_family(self, session):
        family = await families.create_family(session, "group-list")
        await families.create_caregiver(session, family.id, "+15553333333", "Carol")
        await families.create_caregiver(session, family.id, "+15554444444", "Dave")
        caregivers = await families.get_caregivers_for_family(session, family.id)
        assert len(caregivers) == 2
