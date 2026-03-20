"""Tests for ICS feed poller and differ (src/ingestion/ics.py).

Tests ICS parsing with sample .ics content and diff logic.
"""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from src.ingestion.ics import diff_ics_events, parse_ics_feed
from src.state.models import Event

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "ics"


class TestIcsParsing:
    async def test_parse_soccer_schedule(self):
        """Parse the sample soccer schedule ICS fixture."""
        content = (FIXTURES_DIR / "soccer_schedule.ics").read_text()

        events = parse_ics_feed(content)

        assert len(events) == 4

        # Check first event
        practice1 = events[0]
        assert practice1["title"] == "Soccer Practice - U8 Wildcats"
        assert practice1["location"] == "Westfield Park - Field 3"
        assert practice1["uid"] == "soccer-practice-20260321@youthsoccer.org"
        assert practice1["datetime_start"] is not None
        assert practice1["datetime_start"].year == 2026
        assert practice1["datetime_start"].month == 3
        assert practice1["datetime_start"].day == 21

        # Check game event
        game1 = events[1]
        assert "Wildcats vs Thunder" in game1["title"]
        assert game1["location"] == "Riverside Fields - Field A"

    async def test_parse_empty_content(self):
        """Parsing empty content returns empty list."""
        events = parse_ics_feed("")

        assert events == []

    async def test_parse_invalid_ics(self):
        """Parsing invalid ICS content returns empty list (no crash)."""
        events = parse_ics_feed("This is not an ICS file")

        assert events == []

    async def test_parse_minimal_event(self):
        """Parse a minimal valid ICS with one event."""
        content = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//Test//EN
BEGIN:VEVENT
DTSTART:20260401T150000Z
DTEND:20260401T160000Z
SUMMARY:Team Meeting
UID:test-meeting-001@test.com
END:VEVENT
END:VCALENDAR"""

        events = parse_ics_feed(content)

        assert len(events) == 1
        assert events[0]["title"] == "Team Meeting"
        assert events[0]["uid"] == "test-meeting-001@test.com"
        assert events[0]["datetime_start"] == datetime(2026, 4, 1, 15, 0, tzinfo=UTC)
        assert events[0]["datetime_end"] == datetime(2026, 4, 1, 16, 0, tzinfo=UTC)

    async def test_parse_event_without_summary_skipped(self):
        """Events without SUMMARY are skipped."""
        content = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//Test//EN
BEGIN:VEVENT
DTSTART:20260401T150000Z
UID:no-summary@test.com
END:VEVENT
BEGIN:VEVENT
DTSTART:20260401T170000Z
SUMMARY:Has Title
UID:has-summary@test.com
END:VEVENT
END:VCALENDAR"""

        events = parse_ics_feed(content)

        assert len(events) == 1
        assert events[0]["title"] == "Has Title"

    async def test_parse_all_day_event(self):
        """All-day events (DATE instead of DATETIME) are parsed as midnight UTC."""
        content = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//Test//EN
BEGIN:VEVENT
DTSTART;VALUE=DATE:20260401
SUMMARY:Field Day
UID:field-day@test.com
END:VEVENT
END:VCALENDAR"""

        events = parse_ics_feed(content)

        assert len(events) == 1
        assert events[0]["title"] == "Field Day"
        assert events[0]["datetime_start"] == datetime(2026, 4, 1, 0, 0, tzinfo=UTC)


class TestIcsDiff:
    @pytest.fixture
    def family_id(self):
        return uuid4()

    @pytest.fixture
    def mock_session(self):
        return AsyncMock()

    @patch("src.ingestion.ics.event_dal")
    async def test_new_events_detected(self, mock_event_dal, family_id, mock_session):
        """Events with UIDs not in the database are detected as new."""
        current_events = [
            {
                "uid": "new-event-001@test.com",
                "title": "New Practice",
                "datetime_start": datetime(2026, 3, 21, 10, 0, tzinfo=UTC),
            },
            {
                "uid": "new-event-002@test.com",
                "title": "New Game",
                "datetime_start": datetime(2026, 3, 22, 14, 0, tzinfo=UTC),
            },
        ]

        # No existing events match
        mock_event_dal.get_events_by_source_ref = AsyncMock(return_value=[])

        changes = await diff_ics_events(current_events, family_id, mock_session)

        assert len(changes) == 2

    @patch("src.ingestion.ics.event_dal")
    async def test_unchanged_events_not_in_diff(self, mock_event_dal, family_id, mock_session):
        """Events that match existing stored events are not included in diff."""
        dt = datetime(2026, 3, 21, 10, 0, tzinfo=UTC)
        current_events = [
            {
                "uid": "existing-001@test.com",
                "title": "Soccer Practice",
                "datetime_start": dt,
                "location": "Westfield Park",
            },
        ]

        existing_event = MagicMock(spec=Event)
        existing_event.title = "Soccer Practice"
        existing_event.datetime_start = dt
        existing_event.location = "Westfield Park"
        mock_event_dal.get_events_by_source_ref = AsyncMock(return_value=[existing_event])

        changes = await diff_ics_events(current_events, family_id, mock_session)

        assert len(changes) == 0

    @patch("src.ingestion.ics.event_dal")
    async def test_changed_title_detected(self, mock_event_dal, family_id, mock_session):
        """Events with changed titles are detected."""
        dt = datetime(2026, 3, 21, 10, 0, tzinfo=UTC)
        current_events = [
            {
                "uid": "event-001@test.com",
                "title": "Updated Practice Name",
                "datetime_start": dt,
            },
        ]

        existing_event = MagicMock(spec=Event)
        existing_event.title = "Old Practice Name"
        existing_event.datetime_start = dt
        existing_event.location = None
        mock_event_dal.get_events_by_source_ref = AsyncMock(return_value=[existing_event])

        changes = await diff_ics_events(current_events, family_id, mock_session)

        assert len(changes) == 1

    @patch("src.ingestion.ics.event_dal")
    async def test_changed_datetime_detected(self, mock_event_dal, family_id, mock_session):
        """Events with changed start times are detected."""
        dt_old = datetime(2026, 3, 21, 10, 0, tzinfo=UTC)
        dt_new = datetime(2026, 3, 21, 14, 0, tzinfo=UTC)  # 4 hours later

        current_events = [
            {
                "uid": "event-001@test.com",
                "title": "Soccer Practice",
                "datetime_start": dt_new,
            },
        ]

        existing_event = MagicMock(spec=Event)
        existing_event.title = "Soccer Practice"
        existing_event.datetime_start = dt_old
        existing_event.location = None
        mock_event_dal.get_events_by_source_ref = AsyncMock(return_value=[existing_event])

        changes = await diff_ics_events(current_events, family_id, mock_session)

        assert len(changes) == 1

    @patch("src.ingestion.ics.event_dal")
    async def test_events_without_uid_always_in_diff(
        self, mock_event_dal, family_id, mock_session
    ):
        """Events without a UID are always treated as new."""
        current_events = [
            {
                "uid": "",
                "title": "Mystery Event",
                "datetime_start": datetime(2026, 3, 25, 10, 0, tzinfo=UTC),
            },
        ]

        changes = await diff_ics_events(current_events, family_id, mock_session)

        assert len(changes) == 1
        # get_events_by_source_ref should NOT be called for empty UIDs
        mock_event_dal.get_events_by_source_ref.assert_not_called()
