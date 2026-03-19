"""Integration tests verifying tenant isolation — queries never cross families."""

import os
from datetime import UTC, datetime, timedelta

import pytest
from src.state import children as child_dal
from src.state import events as event_dal
from src.state import families
from src.state.models import EventSource, EventType

pytestmark = pytest.mark.skipif(
    os.environ.get("SKIP_DB_TESTS", "1") == "1", reason="Database not available"
)


class TestTenantIsolation:
    async def test_events_isolated_between_families(self, session):
        """Events from family A are never returned when querying family B."""
        family_a = await families.create_family(session, "group-iso-a")
        family_b = await families.create_family(session, "group-iso-b")

        now = datetime.now(UTC)
        await event_dal.create_event(
            session,
            family_a.id,
            source=EventSource.manual,
            type=EventType.sports_practice,
            title="Family A Soccer",
            datetime_start=now + timedelta(days=1),
        )
        await event_dal.create_event(
            session,
            family_b.id,
            source=EventSource.manual,
            type=EventType.birthday_party,
            title="Family B Party",
            datetime_start=now + timedelta(days=1),
        )

        events_a = await event_dal.get_events_in_range(
            session, family_a.id, now, now + timedelta(days=7)
        )
        events_b = await event_dal.get_events_in_range(
            session, family_b.id, now, now + timedelta(days=7)
        )

        assert len(events_a) == 1
        assert events_a[0].title == "Family A Soccer"
        assert len(events_b) == 1
        assert events_b[0].title == "Family B Party"

    async def test_children_isolated_between_families(self, session):
        family_a = await families.create_family(session, "group-child-a")
        family_b = await families.create_family(session, "group-child-b")

        await child_dal.create_child(session, family_a.id, "Alice")
        await child_dal.create_child(session, family_b.id, "Bob")

        children_a = await child_dal.get_children_for_family(session, family_a.id)
        children_b = await child_dal.get_children_for_family(session, family_b.id)

        assert len(children_a) == 1
        assert children_a[0].name == "Alice"
        assert len(children_b) == 1
        assert children_b[0].name == "Bob"

    async def test_caregivers_isolated_between_families(self, session):
        family_a = await families.create_family(session, "group-cg-a")
        family_b = await families.create_family(session, "group-cg-b")

        await families.create_caregiver(session, family_a.id, "+15550001111", "Parent A")
        await families.create_caregiver(session, family_b.id, "+15550002222", "Parent B")

        cg_a = await families.get_caregivers_for_family(session, family_a.id)
        cg_b = await families.get_caregivers_for_family(session, family_b.id)

        assert len(cg_a) == 1
        assert cg_a[0].name == "Parent A"
        assert len(cg_b) == 1
        assert cg_b[0].name == "Parent B"

    async def test_dedup_does_not_cross_families(self, session):
        """Dedup should only find duplicates within the same family."""
        family_a = await families.create_family(session, "group-dedup-a")
        family_b = await families.create_family(session, "group-dedup-b")

        now = datetime.now(UTC)
        await event_dal.create_event(
            session,
            family_a.id,
            source=EventSource.manual,
            type=EventType.sports_practice,
            title="Soccer Practice",
            datetime_start=now + timedelta(days=1),
        )

        # Same title, same time — but different family
        dup = await event_dal.find_duplicate_event(
            session, family_b.id, "Soccer Practice", now + timedelta(days=1)
        )
        assert dup is None
