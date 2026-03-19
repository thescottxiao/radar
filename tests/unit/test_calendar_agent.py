"""Unit tests for the Calendar Coordinator Agent.

Tests conflict detection and event creation parsing with mocked DB and LLM.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from src.agents.calendar import (
    _find_target_event,
    _format_events_for_prompt,
    detect_conflicts,
    handle_schedule,
)
from src.agents.schemas import ExtractedEvent, ResolvedEvent
from src.state.models import Event, EventChild, EventSource, EventType

# ── Fixtures ────────────────────────────────────────────────────────────


def _make_event(
    title: str = "Soccer Practice",
    start_offset_hours: float = 24,
    duration_hours: float = 1.5,
    location: str | None = "Westfield Fields",
    children: list | None = None,
    event_id=None,
) -> MagicMock:
    """Create a mock Event for testing."""
    now = datetime.now(UTC)
    ev = MagicMock(spec=Event)
    ev.id = event_id or uuid4()
    ev.family_id = uuid4()
    ev.title = title
    ev.datetime_start = now + timedelta(hours=start_offset_hours)
    ev.datetime_end = now + timedelta(hours=start_offset_hours + duration_hours)
    ev.location = location
    ev.source = EventSource.manual
    ev.type = EventType.sports_practice
    ev.description = None
    ev.is_recurring = False
    ev.children = children or []
    ev.drop_off_by = None
    ev.pick_up_by = None
    return ev


# ── Conflict detection tests ───────────────────────────────────────────


class TestDetectConflicts:
    """Test the detect_conflicts function with mocked DB."""

    @pytest.mark.asyncio
    async def test_no_conflicts_when_no_overlap(self):
        """Events at different times produce no conflicts."""
        family_id = uuid4()
        session = AsyncMock()

        # Existing event: tomorrow at 2pm
        existing = _make_event(start_offset_hours=24, duration_hours=1.5)

        # New event: tomorrow at 5pm (no overlap with 2pm-3:30pm)
        new_event = ResolvedEvent(
            title="Piano Lesson",
            datetime_start=datetime.now(UTC) + timedelta(hours=29),
            datetime_end=datetime.now(UTC) + timedelta(hours=30),
            location="Music Studio",
        )

        with patch("src.agents.calendar.events_dal.get_events_in_range", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = [existing]
            conflicts = await detect_conflicts(session, family_id, new_event)

        # The search window is ±2 hours from the new event, so the existing
        # event at 24h may or may not be in range. The conflict check is
        # about overlap, not proximity.
        overlapping = [c for c in conflicts if c.conflict_type == "time_overlap"]
        # No overlap: 24-25.5h vs 29-30h
        assert len(overlapping) == 0

    @pytest.mark.asyncio
    async def test_detects_time_overlap(self):
        """Overlapping events are flagged as conflicts."""
        family_id = uuid4()
        session = AsyncMock()

        now = datetime.now(UTC)

        # Existing event: tomorrow at 4pm-5:30pm
        existing = _make_event(start_offset_hours=24, duration_hours=1.5)
        existing.datetime_start = now + timedelta(hours=24)
        existing.datetime_end = now + timedelta(hours=25, minutes=30)

        # New event: tomorrow at 5pm-6pm (overlaps by 30 min)
        new_event = ResolvedEvent(
            title="Piano Lesson",
            datetime_start=now + timedelta(hours=25),
            datetime_end=now + timedelta(hours=26),
        )

        with patch("src.agents.calendar.events_dal.get_events_in_range", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = [existing]
            conflicts = await detect_conflicts(session, family_id, new_event)

        assert len(conflicts) == 1
        assert conflicts[0].conflict_type == "time_overlap"
        assert "Soccer Practice" in conflicts[0].description

    @pytest.mark.asyncio
    async def test_detects_child_double_book(self):
        """Same child in two overlapping events is flagged."""
        family_id = uuid4()
        child_id = uuid4()
        session = AsyncMock()

        now = datetime.now(UTC)

        # Existing event with a child linked
        existing = _make_event(start_offset_hours=24, duration_hours=1.5)
        existing.datetime_start = now + timedelta(hours=24)
        existing.datetime_end = now + timedelta(hours=25, minutes=30)
        child_link = MagicMock(spec=EventChild)
        child_link.child_id = child_id
        existing.children = [child_link]

        # New event for same child, overlapping time
        new_event = ResolvedEvent(
            title="Piano Lesson",
            datetime_start=now + timedelta(hours=25),
            datetime_end=now + timedelta(hours=26),
            child_ids=[child_id],
        )

        with patch("src.agents.calendar.events_dal.get_events_in_range", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = [existing]
            conflicts = await detect_conflicts(session, family_id, new_event)

        assert len(conflicts) == 1
        assert conflicts[0].conflict_type == "child_double_book"

    @pytest.mark.asyncio
    async def test_location_impossible_conflict(self):
        """Same child at two different locations in overlapping time."""
        family_id = uuid4()
        child_id = uuid4()
        session = AsyncMock()

        now = datetime.now(UTC)

        existing = _make_event(
            start_offset_hours=24,
            duration_hours=1.5,
            location="Westfield Fields",
        )
        existing.datetime_start = now + timedelta(hours=24)
        existing.datetime_end = now + timedelta(hours=25, minutes=30)
        child_link = MagicMock(spec=EventChild)
        child_link.child_id = child_id
        existing.children = [child_link]

        new_event = ResolvedEvent(
            title="Piano Lesson",
            datetime_start=now + timedelta(hours=25),
            datetime_end=now + timedelta(hours=26),
            location="Music Studio",
            child_ids=[child_id],
        )

        with patch("src.agents.calendar.events_dal.get_events_in_range", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = [existing]
            conflicts = await detect_conflicts(session, family_id, new_event)

        assert len(conflicts) == 1
        assert conflicts[0].conflict_type == "location_impossible"

    @pytest.mark.asyncio
    async def test_no_conflict_same_location(self):
        """Same location overlap is just time_overlap, not location_impossible."""
        family_id = uuid4()
        child_id = uuid4()
        session = AsyncMock()

        now = datetime.now(UTC)

        existing = _make_event(
            start_offset_hours=24,
            duration_hours=1.5,
            location="Westfield Fields",
        )
        existing.datetime_start = now + timedelta(hours=24)
        existing.datetime_end = now + timedelta(hours=25, minutes=30)
        child_link = MagicMock(spec=EventChild)
        child_link.child_id = child_id
        existing.children = [child_link]

        new_event = ResolvedEvent(
            title="Soccer Game",
            datetime_start=now + timedelta(hours=25),
            datetime_end=now + timedelta(hours=26),
            location="Westfield Fields",
            child_ids=[child_id],
        )

        with patch("src.agents.calendar.events_dal.get_events_in_range", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = [existing]
            conflicts = await detect_conflicts(session, family_id, new_event)

        assert len(conflicts) == 1
        # Same location + same child = child_double_book (not location_impossible)
        assert conflicts[0].conflict_type == "child_double_book"

    @pytest.mark.asyncio
    async def test_empty_calendar_no_conflicts(self):
        """No existing events means no conflicts."""
        family_id = uuid4()
        session = AsyncMock()

        new_event = ResolvedEvent(
            title="Soccer Practice",
            datetime_start=datetime.now(UTC) + timedelta(hours=24),
            datetime_end=datetime.now(UTC) + timedelta(hours=25, minutes=30),
        )

        with patch("src.agents.calendar.events_dal.get_events_in_range", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = []
            conflicts = await detect_conflicts(session, family_id, new_event)

        assert len(conflicts) == 0


# ── Event creation request parsing tests ───────────────────────────────


class TestHandleSchedule:
    """Test handle_schedule with mocked LLM and DB."""

    @pytest.mark.asyncio
    async def test_creates_event_from_message(self):
        """Verify that handle_schedule extracts and creates an event."""
        family_id = uuid4()
        sender_id = uuid4()
        session = AsyncMock()

        now = datetime.now(UTC)
        next_tuesday = now + timedelta(days=(1 - now.weekday()) % 7 + 1)
        start_time = next_tuesday.replace(hour=16, minute=0, second=0, microsecond=0)

        mock_family = MagicMock()
        mock_family.timezone = "America/New_York"

        mock_child = MagicMock()
        mock_child.id = uuid4()
        mock_child.name = "Emma"
        mock_child.activities = ["soccer"]

        mock_caregiver = MagicMock()
        mock_caregiver.id = sender_id
        mock_caregiver.name = "Sarah"
        mock_caregiver.whatsapp_phone = "+15551234567"
        mock_caregiver.google_refresh_token_encrypted = None

        mock_event = MagicMock(spec=Event)
        mock_event.id = uuid4()
        mock_event.title = "Soccer Practice"
        mock_event.datetime_start = start_time

        extracted = ExtractedEvent(
            title="Soccer Practice",
            event_type="sports_practice",
            date_str="next Tuesday",
            time_str="4pm",
            child_names=["Emma"],
            location="Westfield Fields",
        )

        with (
            patch("src.agents.calendar.families_dal.get_family", new_callable=AsyncMock, return_value=mock_family),
            patch("src.agents.calendar.children_dal.get_children_for_family", new_callable=AsyncMock, return_value=[mock_child]),
            patch("src.agents.calendar.families_dal.get_caregivers_for_family", new_callable=AsyncMock, return_value=[mock_caregiver]),
            patch("src.agents.calendar.events_dal.get_upcoming_events", new_callable=AsyncMock, return_value=[]),
            patch("src.agents.calendar.events_dal.get_events_in_range", new_callable=AsyncMock, return_value=[]),
            patch("src.agents.calendar.extract", new_callable=AsyncMock, return_value=extracted),
            patch("src.agents.calendar.generate", new_callable=AsyncMock, return_value=start_time.isoformat()),
            patch("src.agents.calendar.events_dal.find_duplicate_event", new_callable=AsyncMock, return_value=None),
            patch("src.agents.calendar.events_dal.create_event", new_callable=AsyncMock, return_value=mock_event),
            patch("src.agents.calendar.events_dal.link_children_to_event", new_callable=AsyncMock),
            patch("src.agents.calendar.children_dal.fuzzy_match_child", new_callable=AsyncMock, return_value=mock_child),
            patch("src.agents.calendar.memory_dal.store_message", new_callable=AsyncMock),
        ):
            result = await handle_schedule(
                session,
                family_id,
                "Add soccer practice next Tuesday at 4pm at Westfield Fields for Emma",
                sender_id,
            )

        assert "Soccer Practice" in result
        assert "Westfield Fields" in result or "added" in result.lower()

    @pytest.mark.asyncio
    async def test_duplicate_detection(self):
        """Verify that handle_schedule detects duplicates."""
        family_id = uuid4()
        sender_id = uuid4()
        session = AsyncMock()

        now = datetime.now(UTC)
        start_time = now + timedelta(days=1, hours=4)

        mock_family = MagicMock()
        mock_family.timezone = "America/New_York"

        mock_caregiver = MagicMock()
        mock_caregiver.id = sender_id
        mock_caregiver.name = "Sarah"
        mock_caregiver.whatsapp_phone = "+15551234567"
        mock_caregiver.google_refresh_token_encrypted = None

        existing_event = MagicMock(spec=Event)
        existing_event.id = uuid4()
        existing_event.title = "Soccer Practice"
        existing_event.datetime_start = start_time

        extracted = ExtractedEvent(
            title="Soccer Practice",
            event_type="sports_practice",
            date_str="tomorrow",
            time_str="4pm",
        )

        with (
            patch("src.agents.calendar.families_dal.get_family", new_callable=AsyncMock, return_value=mock_family),
            patch("src.agents.calendar.children_dal.get_children_for_family", new_callable=AsyncMock, return_value=[]),
            patch("src.agents.calendar.families_dal.get_caregivers_for_family", new_callable=AsyncMock, return_value=[mock_caregiver]),
            patch("src.agents.calendar.events_dal.get_upcoming_events", new_callable=AsyncMock, return_value=[existing_event]),
            patch("src.agents.calendar.events_dal.get_events_in_range", new_callable=AsyncMock, return_value=[]),
            patch("src.agents.calendar.extract", new_callable=AsyncMock, return_value=extracted),
            patch("src.agents.calendar.generate", new_callable=AsyncMock, return_value=start_time.isoformat()),
            patch("src.agents.calendar.events_dal.find_duplicate_event", new_callable=AsyncMock, return_value=existing_event),
        ):
            result = await handle_schedule(
                session,
                family_id,
                "Add soccer practice tomorrow at 4pm",
                sender_id,
            )

        assert "already" in result.lower() or "duplicate" in result.lower()


# ── Helper function tests ──────────────────────────────────────────────


class TestFindTargetEvent:
    """Test the _find_target_event helper."""

    @pytest.mark.asyncio
    async def test_exact_match(self):
        session = AsyncMock()
        family_id = uuid4()
        ev = _make_event(title="Soccer Practice")
        result = await _find_target_event(session, family_id, "Soccer Practice", [ev])
        assert result is not None
        assert result.title == "Soccer Practice"

    @pytest.mark.asyncio
    async def test_partial_match(self):
        session = AsyncMock()
        family_id = uuid4()
        ev = _make_event(title="Soccer Practice")
        result = await _find_target_event(session, family_id, "soccer", [ev])
        assert result is not None

    @pytest.mark.asyncio
    async def test_no_match(self):
        session = AsyncMock()
        family_id = uuid4()
        ev = _make_event(title="Soccer Practice")
        result = await _find_target_event(session, family_id, "dentist appointment", [ev])
        assert result is None

    @pytest.mark.asyncio
    async def test_best_match_selected(self):
        session = AsyncMock()
        family_id = uuid4()
        ev1 = _make_event(title="Soccer Practice")
        ev2 = _make_event(title="Soccer Game")
        result = await _find_target_event(session, family_id, "soccer practice", [ev1, ev2])
        assert result is not None
        assert result.title == "Soccer Practice"


class TestFormatEventsForPrompt:
    def test_empty_list(self):
        assert _format_events_for_prompt([]) == "(no upcoming events)"

    def test_formats_events(self):
        ev = _make_event(title="Soccer Practice", location="Field A")
        result = _format_events_for_prompt([ev])
        assert "Soccer Practice" in result
        assert "Field A" in result
