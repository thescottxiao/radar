"""Unit tests for the Calendar Change Detector.

Tests GCal event to Radar event mapping and change classification logic.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from src.extraction.calendar import (
    _infer_event_type,
    _parse_gcal_datetime,
    gcal_event_to_radar_event,
    process_calendar_change,
)
from src.state.models import Event, EventSource, EventType

# ── GCal event mapping tests ──────────────────────────────────────────


class TestGcalEventToRadarEvent:
    """Test mapping from GCal event dict to Radar event kwargs."""

    def test_basic_mapping(self):
        family_id = uuid4()
        caregiver_id = uuid4()

        gcal_event = {
            "id": "abc123",
            "summary": "Soccer Practice",
            "description": "Bring shin guards",
            "location": "Westfield Fields",
            "start": {"dateTime": "2026-03-25T16:00:00-04:00"},
            "end": {"dateTime": "2026-03-25T17:30:00-04:00"},
            "status": "confirmed",
        }

        result = gcal_event_to_radar_event(gcal_event, family_id, caregiver_id)

        assert result["title"] == "Soccer Practice"
        assert result["description"] == "Bring shin guards"
        assert result["location"] == "Westfield Fields"
        assert result["source"] == EventSource.calendar
        assert result["source_refs"] == ["gcal:abc123"]
        assert result["type"] == EventType.sports_practice
        assert result["confirmed_by_caregiver"] is True
        assert result["extraction_confidence"] == 1.0
        assert result["is_recurring"] is False

    def test_all_day_event(self):
        family_id = uuid4()
        caregiver_id = uuid4()

        gcal_event = {
            "id": "allday123",
            "summary": "School Field Trip",
            "start": {"date": "2026-03-25"},
            "end": {"date": "2026-03-26"},
        }

        result = gcal_event_to_radar_event(gcal_event, family_id, caregiver_id)

        assert result["title"] == "School Field Trip"
        assert result["datetime_start"] is not None
        assert result["datetime_start"].hour == 0  # Midnight for all-day

    def test_recurring_event(self):
        family_id = uuid4()
        caregiver_id = uuid4()

        gcal_event = {
            "id": "instance_20260325",
            "summary": "Piano Lesson",
            "start": {"dateTime": "2026-03-25T15:00:00-04:00"},
            "end": {"dateTime": "2026-03-25T16:00:00-04:00"},
            "recurringEventId": "recurring_piano_base",
        }

        result = gcal_event_to_radar_event(gcal_event, family_id, caregiver_id)

        assert result["is_recurring"] is True

    def test_missing_fields_use_defaults(self):
        family_id = uuid4()
        caregiver_id = uuid4()

        gcal_event = {
            "id": "minimal",
            "start": {"dateTime": "2026-03-25T10:00:00-04:00"},
        }

        result = gcal_event_to_radar_event(gcal_event, family_id, caregiver_id)

        assert result["title"] == "Untitled Event"
        assert result["description"] is None
        assert result["location"] is None
        assert result["type"] == EventType.other

    def test_event_type_inference(self):
        family_id = uuid4()
        caregiver_id = uuid4()

        test_cases = [
            ("Emma's Birthday Party", EventType.birthday_party),
            ("Soccer Practice", EventType.sports_practice),
            ("Basketball Game", EventType.sports_game),
            ("School Open House", EventType.school_event),
            ("Summer Camp", EventType.camp),
            ("Playdate with Max", EventType.playdate),
            ("Doctor Appointment", EventType.medical_appointment),
            ("Dentist Checkup", EventType.dental_appointment),
            ("Piano Recital", EventType.recital_performance),
            ("Registration Deadline", EventType.registration_deadline),
            ("Random Meeting", EventType.other),
        ]

        for summary, expected_type in test_cases:
            gcal_event = {
                "id": f"test_{summary}",
                "summary": summary,
                "start": {"dateTime": "2026-03-25T10:00:00-04:00"},
            }
            result = gcal_event_to_radar_event(gcal_event, family_id, caregiver_id)
            assert result["type"] == expected_type, f"Failed for '{summary}': expected {expected_type}, got {result['type']}"


# ── Datetime parsing tests ─────────────────────────────────────────────


class TestParseGcalDatetime:

    def test_datetime_with_timezone(self):
        dt = _parse_gcal_datetime({"dateTime": "2026-03-25T16:00:00-04:00"})
        assert dt is not None
        assert dt.year == 2026
        assert dt.month == 3
        assert dt.day == 25

    def test_date_only(self):
        dt = _parse_gcal_datetime({"date": "2026-03-25"})
        assert dt is not None
        assert dt.hour == 0
        assert dt.minute == 0

    def test_empty_dict(self):
        assert _parse_gcal_datetime({}) is None

    def test_none(self):
        assert _parse_gcal_datetime(None) is None


# ── Event type inference tests ─────────────────────────────────────────


class TestInferEventType:

    def test_birthday(self):
        assert _infer_event_type("Sophia's Birthday Party") == EventType.birthday_party

    def test_practice(self):
        assert _infer_event_type("Soccer Practice") == EventType.sports_practice

    def test_game(self):
        assert _infer_event_type("Basketball Game") == EventType.sports_game

    def test_case_insensitive(self):
        assert _infer_event_type("SOCCER PRACTICE") == EventType.sports_practice

    def test_unknown(self):
        assert _infer_event_type("Weekly Team Sync") == EventType.other


# ── Change classification tests ────────────────────────────────────────


class TestProcessCalendarChange:
    """Test change classification logic."""

    @pytest.mark.asyncio
    async def test_new_event_created(self):
        """A new GCal event with no matching Radar event gets created."""
        family_id = uuid4()
        caregiver_id = uuid4()
        session = AsyncMock()

        gcal_event = {
            "id": "new123",
            "summary": "Swim Lessons",
            "start": {"dateTime": "2026-03-25T10:00:00-04:00"},
            "end": {"dateTime": "2026-03-25T11:00:00-04:00"},
            "location": "YMCA Pool",
            "status": "confirmed",
        }

        mock_event = MagicMock(spec=Event)
        mock_event.id = uuid4()
        mock_event.title = "Swim Lessons"
        mock_event.datetime_start = datetime(2026, 3, 25, 14, 0, tzinfo=UTC)
        mock_event.location = "YMCA Pool"

        with (
            patch("src.extraction.calendar.events_dal.get_events_by_source_ref", new_callable=AsyncMock, return_value=[]),
            patch("src.extraction.calendar.events_dal.find_duplicate_event", new_callable=AsyncMock, return_value=None),
            patch("src.extraction.calendar.events_dal.create_event", new_callable=AsyncMock, return_value=mock_event),
        ):
            result = await process_calendar_change(
                session, family_id, gcal_event, caregiver_id
            )

        assert result is not None
        assert result["change_type"] == "new_event"
        assert result["notification"] is not None
        assert "Swim Lessons" in result["notification"]

    @pytest.mark.asyncio
    async def test_duplicate_event_not_created(self):
        """A GCal event that matches an existing Radar event is not duplicated."""
        family_id = uuid4()
        caregiver_id = uuid4()
        session = AsyncMock()

        existing = MagicMock(spec=Event)
        existing.id = uuid4()
        existing.title = "Swim Lessons"
        existing.source_refs = []

        gcal_event = {
            "id": "dup123",
            "summary": "Swim Lessons",
            "start": {"dateTime": "2026-03-25T10:00:00-04:00"},
            "end": {"dateTime": "2026-03-25T11:00:00-04:00"},
            "status": "confirmed",
        }

        with (
            patch("src.extraction.calendar.events_dal.get_events_by_source_ref", new_callable=AsyncMock, return_value=[]),
            patch("src.extraction.calendar.events_dal.find_duplicate_event", new_callable=AsyncMock, return_value=existing),
            patch("src.extraction.calendar.events_dal.update_event", new_callable=AsyncMock),
        ):
            result = await process_calendar_change(
                session, family_id, gcal_event, caregiver_id
            )

        assert result is not None
        assert result["change_type"] == "no_action"

    @pytest.mark.asyncio
    async def test_cancellation(self):
        """A cancelled GCal event marks the Radar event as cancelled."""
        family_id = uuid4()
        caregiver_id = uuid4()
        session = AsyncMock()

        existing = MagicMock(spec=Event)
        existing.id = uuid4()
        existing.title = "Swim Lessons"
        existing.datetime_start = datetime(2026, 3, 25, 14, 0, tzinfo=UTC)
        existing.description = None
        existing.is_recurring = False
        existing.recurring_schedule_id = None

        gcal_event = {
            "id": "cancel123",
            "summary": "Swim Lessons",
            "status": "cancelled",
        }

        with (
            patch("src.extraction.calendar.events_dal.get_events_by_source_ref", new_callable=AsyncMock, return_value=[existing]),
            patch("src.extraction.calendar.events_dal.update_event", new_callable=AsyncMock),
        ):
            result = await process_calendar_change(
                session, family_id, gcal_event, caregiver_id
            )

        assert result is not None
        assert result["change_type"] == "cancellation"
        assert "cancelled" in result["notification"].lower()

    @pytest.mark.asyncio
    async def test_time_change(self):
        """An updated GCal event time is detected and applied."""
        family_id = uuid4()
        caregiver_id = uuid4()
        session = AsyncMock()

        old_start = datetime(2026, 3, 25, 14, 0, tzinfo=UTC)
        new_start_str = "2026-03-25T16:00:00-04:00"  # 4pm EDT = 20:00 UTC

        existing = MagicMock(spec=Event)
        existing.id = uuid4()
        existing.title = "Swim Lessons"
        existing.datetime_start = old_start
        existing.datetime_end = datetime(2026, 3, 25, 15, 0, tzinfo=UTC)
        existing.location = "YMCA Pool"
        existing.description = None

        gcal_event = {
            "id": "update123",
            "summary": "Swim Lessons",
            "start": {"dateTime": new_start_str},
            "end": {"dateTime": "2026-03-25T17:00:00-04:00"},
            "location": "YMCA Pool",
            "status": "confirmed",
        }

        with (
            patch("src.extraction.calendar.events_dal.get_events_by_source_ref", new_callable=AsyncMock, return_value=[existing]),
            patch("src.extraction.calendar.events_dal.update_event", new_callable=AsyncMock),
        ):
            result = await process_calendar_change(
                session, family_id, gcal_event, caregiver_id
            )

        assert result is not None
        assert result["change_type"] == "time_change"
        assert result["notification"] is not None
        assert "time changed" in result["notification"].lower()

    @pytest.mark.asyncio
    async def test_location_change(self):
        """An updated GCal event location is detected."""
        family_id = uuid4()
        caregiver_id = uuid4()
        session = AsyncMock()

        start = datetime(2026, 3, 25, 14, 0, tzinfo=UTC)

        existing = MagicMock(spec=Event)
        existing.id = uuid4()
        existing.title = "Swim Lessons"
        existing.datetime_start = start
        existing.datetime_end = datetime(2026, 3, 25, 15, 0, tzinfo=UTC)
        existing.location = "YMCA Pool"
        existing.description = None

        gcal_event = {
            "id": "locchange123",
            "summary": "Swim Lessons",
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": "2026-03-25T15:00:00+00:00"},
            "location": "Community Center Pool",
            "status": "confirmed",
        }

        with (
            patch("src.extraction.calendar.events_dal.get_events_by_source_ref", new_callable=AsyncMock, return_value=[existing]),
            patch("src.extraction.calendar.events_dal.update_event", new_callable=AsyncMock),
        ):
            result = await process_calendar_change(
                session, family_id, gcal_event, caregiver_id
            )

        assert result is not None
        assert result["change_type"] == "location_change"
        assert "Community Center Pool" in result["notification"]

    @pytest.mark.asyncio
    async def test_no_changes_detected(self):
        """An event with no actual changes returns no_action."""
        family_id = uuid4()
        caregiver_id = uuid4()
        session = AsyncMock()

        start = datetime(2026, 3, 25, 14, 0, tzinfo=UTC)
        end = datetime(2026, 3, 25, 15, 0, tzinfo=UTC)

        existing = MagicMock(spec=Event)
        existing.id = uuid4()
        existing.title = "Swim Lessons"
        existing.datetime_start = start
        existing.datetime_end = end
        existing.location = "YMCA Pool"
        existing.description = "Bring goggles"

        gcal_event = {
            "id": "nochange123",
            "summary": "Swim Lessons",
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()},
            "location": "YMCA Pool",
            "description": "Bring goggles",
            "status": "confirmed",
        }

        with patch("src.extraction.calendar.events_dal.get_events_by_source_ref", new_callable=AsyncMock, return_value=[existing]):
            result = await process_calendar_change(
                session, family_id, gcal_event, caregiver_id
            )

        assert result is not None
        assert result["change_type"] == "no_action"
        assert result["notification"] is None
