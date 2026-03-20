import os
from datetime import UTC, datetime, timedelta

import pytest
from src.state import events as event_dal
from src.state.models import EventSource

pytestmark = pytest.mark.skipif(
    os.environ.get("SKIP_DB_TESTS", "1") == "1", reason="Database not available"
)


class TestEvents:
    async def test_create_event(self, session, sample_family):
        family = sample_family["family"]
        now = datetime.now(UTC)
        event = await event_dal.create_event(
            session,
            family.id,
            source=EventSource.manual,
            type="sports_practice",
            title="Basketball Practice",
            datetime_start=now + timedelta(days=1),
        )
        assert event.id is not None
        assert event.title == "Basketball Practice"
        assert event.family_id == family.id

    async def test_get_events_in_range(self, session, sample_events, sample_family):
        family = sample_family["family"]
        now = datetime.now(UTC)
        found = await event_dal.get_events_in_range(
            session, family.id, now, now + timedelta(days=7)
        )
        assert len(found) == 3

    async def test_get_events_in_range_filtered(self, session, sample_events, sample_family):
        family = sample_family["family"]
        now = datetime.now(UTC)
        # Only events in the next 2 days
        found = await event_dal.get_events_in_range(
            session, family.id, now, now + timedelta(days=2)
        )
        assert len(found) == 1  # Only soccer practice

    async def test_find_duplicate_exact(self, session, sample_events, sample_family):
        family = sample_family["family"]
        soccer = sample_events[0]
        dup = await event_dal.find_duplicate_event(
            session, family.id, "Soccer Practice", soccer.datetime_start
        )
        assert dup is not None
        assert dup.id == soccer.id

    async def test_find_duplicate_close_time(self, session, sample_events, sample_family):
        family = sample_family["family"]
        soccer = sample_events[0]
        # 20 minutes off, same title
        dup = await event_dal.find_duplicate_event(
            session,
            family.id,
            "Soccer Practice",
            soccer.datetime_start + timedelta(minutes=20),
        )
        assert dup is not None

    async def test_find_duplicate_no_match_time(self, session, sample_events, sample_family):
        family = sample_family["family"]
        soccer = sample_events[0]
        # 60 minutes off — outside 30-min window
        dup = await event_dal.find_duplicate_event(
            session,
            family.id,
            "Soccer Practice",
            soccer.datetime_start + timedelta(minutes=60),
        )
        assert dup is None

    async def test_find_duplicate_no_match_title(self, session, sample_events, sample_family):
        family = sample_family["family"]
        soccer = sample_events[0]
        # Same time, different title
        dup = await event_dal.find_duplicate_event(
            session,
            family.id,
            "Piano Lesson",
            soccer.datetime_start,
        )
        assert dup is None

    async def test_get_events_needing_rsvp(self, session, sample_events, sample_family):
        family = sample_family["family"]
        rsvp_events = await event_dal.get_events_needing_rsvp(session, family.id)
        assert len(rsvp_events) == 1
        assert rsvp_events[0].title == "Sophia's Birthday Party"

    async def test_update_event(self, session, sample_events, sample_family):
        family = sample_family["family"]
        event = sample_events[0]
        updated = await event_dal.update_event(
            session, family.id, event.id, location="New Field"
        )
        assert updated.location == "New Field"
