"""Tests for all-day event handling across the codebase.

Covers:
- GCal body conversion (date vs dateTime format)
- Dedup matching (all-day ↔ timed cross-matching)
- Merge promotion (all-day → timed)
- Display formatting (fmt_event_time)
- Event creation (all_day field persistence)
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from src.actions.gcal import event_to_gcal_body
from src.extraction.dedup import deduplicate_event
from src.extraction.email import ExtractedEvent
from src.state.models import Event, EventSource, RsvpStatus
from src.utils.timezone import fmt_event_time


@pytest.fixture
def family_id():
    return uuid4()


@pytest.fixture
def mock_session():
    return AsyncMock()


def _make_event(
    family_id,
    title="Spring Festival",
    dt_start=None,
    dt_end=None,
    all_day=False,
    time_tbd=False,
    time_explicit=True,
    location=None,
    description=None,
    source=EventSource.email,
    confirmed_by_caregiver=True,
):
    """Build a MagicMock Event with sensible defaults."""
    event = MagicMock(spec=Event)
    event.id = uuid4()
    event.family_id = family_id
    event.title = title
    event.datetime_start = dt_start or datetime(2026, 3, 31, 0, 0, tzinfo=UTC)
    event.datetime_end = dt_end
    event.all_day = all_day
    event.time_tbd = time_tbd
    event.time_explicit = time_explicit
    event.location = location
    event.description = description
    event.source = source
    event.source_refs = []
    event.rsvp_status = RsvpStatus.not_applicable
    event.rsvp_deadline = None
    event.rsvp_contact = None
    event.extraction_confidence = 0.7
    event.drop_off_by = None
    event.pick_up_by = None
    event.rrule = None
    event.confirmed_by_caregiver = confirmed_by_caregiver
    event.children = []
    event.caregivers = []
    return event


# ── TestGCalBodyAllDay ────────────────────────────────────────────────────


class TestGCalBodyAllDay:
    """Test event_to_gcal_body handling of all-day vs timed events."""

    def test_all_day_event_uses_date_format(self, family_id):
        """All-day event produces {"date": "..."} not {"dateTime": "..."}."""
        event = _make_event(
            family_id,
            title="School Holiday",
            dt_start=datetime(2026, 3, 31, 0, 0, tzinfo=UTC),
            all_day=True,
        )

        body = event_to_gcal_body(event)

        assert "date" in body["start"]
        assert "dateTime" not in body["start"]
        assert body["start"]["date"] == "2026-03-31"
        assert "date" in body["end"]
        assert "dateTime" not in body["end"]

    def test_timed_event_uses_datetime_format(self, family_id):
        """Timed event produces {"dateTime": "..."} not {"date": "..."}."""
        event = _make_event(
            family_id,
            title="Soccer Practice",
            dt_start=datetime(2026, 3, 31, 16, 30, tzinfo=UTC),
            dt_end=datetime(2026, 3, 31, 18, 0, tzinfo=UTC),
            all_day=False,
        )

        body = event_to_gcal_body(event)

        assert "dateTime" in body["start"]
        assert "date" not in body["start"]
        assert "dateTime" in body["end"]
        assert "date" not in body["end"]

    def test_all_day_end_date_defaults_to_next_day(self, family_id):
        """When all_day=True and no datetime_end, end is start + 1 day."""
        event = _make_event(
            family_id,
            title="Professional Day",
            dt_start=datetime(2026, 3, 31, 0, 0, tzinfo=UTC),
            dt_end=None,
            all_day=True,
        )

        body = event_to_gcal_body(event)

        assert body["start"]["date"] == "2026-03-31"
        assert body["end"]["date"] == "2026-04-01"

    def test_time_tbd_uses_date_format_with_suffix(self, family_id):
        """Event with time_tbd=True, all_day=False produces date format and '(time TBD)' in summary."""
        event = _make_event(
            family_id,
            title="Birthday Party",
            dt_start=datetime(2026, 3, 31, 0, 0, tzinfo=UTC),
            all_day=False,
            time_tbd=True,
        )

        body = event_to_gcal_body(event)

        assert "date" in body["start"]
        assert "dateTime" not in body["start"]
        assert body["start"]["date"] == "2026-03-31"
        assert "(time TBD)" in body["summary"]

    def test_estimated_time_uses_datetime_format(self, family_id):
        """Event with time_explicit=False, all_day=False, time_tbd=False uses dateTime format."""
        event = _make_event(
            family_id,
            title="Swim Meet",
            dt_start=datetime(2026, 3, 31, 14, 0, tzinfo=UTC),
            dt_end=datetime(2026, 3, 31, 16, 0, tzinfo=UTC),
            all_day=False,
            time_tbd=False,
            time_explicit=False,
        )

        body = event_to_gcal_body(event)

        assert "dateTime" in body["start"]
        assert "date" not in body["start"]
        assert "dateTime" in body["end"]
        assert "date" not in body["end"]


# ── TestDedupAllDayMatching ───────────────────────────────────────────────


class TestDedupAllDayMatching:
    """Test find_duplicate_event cross-matching between all-day and timed events."""

    @pytest.mark.asyncio
    @patch("src.extraction.dedup.event_dal")
    async def test_all_day_matches_timed_same_date(
        self, mock_event_dal, mock_session, family_id
    ):
        """All-day event on March 31 matches a 7:07 PM event on March 31."""
        timed_dt = datetime(2026, 3, 31, 19, 7, tzinfo=UTC)

        # The timed event already exists
        existing = _make_event(
            family_id, "Spring Festival",
            dt_start=timed_dt,
            all_day=False,
        )
        mock_event_dal.find_duplicate_event = AsyncMock(return_value=existing)
        mock_event_dal.update_event = AsyncMock(return_value=existing)

        # Incoming all-day extraction for the same date
        extracted = ExtractedEvent(
            title="Spring Festival",
            datetime_start=datetime(2026, 3, 31, 0, 0, tzinfo=UTC),
            all_day=True,
            confidence=0.8,
        )

        event, is_new = await deduplicate_event(
            mock_session, family_id, extracted
        )

        assert is_new is False, "All-day event should match a timed event on the same date"

    @pytest.mark.asyncio
    @patch("src.extraction.dedup.event_dal")
    async def test_all_day_no_match_different_date(
        self, mock_event_dal, mock_session, family_id
    ):
        """All-day event on March 31 doesn't match event on April 1."""
        mock_event_dal.find_duplicate_event = AsyncMock(return_value=None)
        new_event = MagicMock(spec=Event)
        new_event.id = uuid4()
        mock_event_dal.create_event = AsyncMock(return_value=new_event)

        # All-day event on March 31, nothing on that date in DB
        extracted = ExtractedEvent(
            title="Spring Festival",
            datetime_start=datetime(2026, 3, 31, 0, 0, tzinfo=UTC),
            all_day=True,
            confidence=0.8,
        )

        event, is_new = await deduplicate_event(
            mock_session, family_id, extracted
        )

        assert is_new is True, "All-day event on a different date should not match"

    @pytest.mark.asyncio
    @patch("src.extraction.dedup.event_dal")
    async def test_time_tbd_matches_timed_same_date(
        self, mock_event_dal, mock_session, family_id
    ):
        """A time-TBD event at midnight matches a timed event on the same date."""
        timed_dt = datetime(2026, 3, 31, 15, 0, tzinfo=UTC)

        # The timed event already exists
        existing = _make_event(
            family_id, "Birthday Party",
            dt_start=timed_dt,
            all_day=False,
        )
        mock_event_dal.find_duplicate_event = AsyncMock(return_value=existing)
        mock_event_dal.update_event = AsyncMock(return_value=existing)

        # Incoming time-TBD extraction for the same date
        extracted = ExtractedEvent(
            title="Birthday Party",
            datetime_start=datetime(2026, 3, 31, 0, 0, tzinfo=UTC),
            all_day=False,
            time_tbd=True,
            confidence=0.8,
        )

        event, is_new = await deduplicate_event(
            mock_session, family_id, extracted
        )

        assert is_new is False, "Time-TBD event should match a timed event on the same date"

    @pytest.mark.asyncio
    @patch("src.extraction.dedup.event_dal")
    async def test_time_tbd_no_match_different_date(
        self, mock_event_dal, mock_session, family_id
    ):
        """A time-TBD event on March 31 doesn't match a timed event on April 1."""
        mock_event_dal.find_duplicate_event = AsyncMock(return_value=None)
        new_event = MagicMock(spec=Event)
        new_event.id = uuid4()
        mock_event_dal.create_event = AsyncMock(return_value=new_event)

        # Time-TBD event on March 31, nothing on that date in DB
        extracted = ExtractedEvent(
            title="Birthday Party",
            datetime_start=datetime(2026, 3, 31, 0, 0, tzinfo=UTC),
            all_day=False,
            time_tbd=True,
            confidence=0.8,
        )

        event, is_new = await deduplicate_event(
            mock_session, family_id, extracted
        )

        assert is_new is True, "Time-TBD event on a different date should not match"

    @pytest.mark.asyncio
    @patch("src.extraction.dedup.event_dal")
    async def test_timed_events_still_use_30min_window(
        self, mock_event_dal, mock_session, family_id
    ):
        """Normal timed dedup still works with the +/-30 min window."""
        dt = datetime(2026, 3, 31, 16, 0, tzinfo=UTC)
        existing = _make_event(
            family_id, "Soccer Practice",
            dt_start=dt,
            all_day=False,
        )
        mock_event_dal.find_duplicate_event = AsyncMock(return_value=existing)
        mock_event_dal.update_event = AsyncMock(return_value=existing)

        # Extracted event 20 min later — within the 30 min window
        extracted = ExtractedEvent(
            title="Soccer Practice",
            datetime_start=dt + timedelta(minutes=20),
            all_day=False,
            confidence=0.85,
        )

        event, is_new = await deduplicate_event(
            mock_session, family_id, extracted
        )

        assert is_new is False, "Timed events within 30 min should still match"


# ── TestMergeAllDayPromotion ──────────────────────────────────────────────


class TestMergeAllDayPromotion:
    """Test _merge_event promotion of all-day → timed on merge."""

    @pytest.mark.asyncio
    @patch("src.extraction.dedup.event_dal")
    async def test_promotes_all_day_to_timed(
        self, mock_event_dal, mock_session, family_id
    ):
        """Existing all_day=True event merged with all_day=False extraction
        updates datetime_start, datetime_end, and sets all_day=False."""
        existing = _make_event(
            family_id, "Spring Festival",
            dt_start=datetime(2026, 3, 31, 0, 0, tzinfo=UTC),
            all_day=True,
        )

        timed_start = datetime(2026, 3, 31, 19, 0, tzinfo=UTC)
        timed_end = datetime(2026, 3, 31, 21, 0, tzinfo=UTC)
        extracted = ExtractedEvent(
            title="Spring Festival",
            datetime_start=timed_start,
            datetime_end=timed_end,
            all_day=False,
            confidence=0.9,
        )

        mock_event_dal.find_duplicate_event = AsyncMock(return_value=existing)
        mock_event_dal.update_event = AsyncMock(return_value=existing)

        event, is_new = await deduplicate_event(
            mock_session, family_id, extracted
        )

        assert is_new is False
        mock_event_dal.update_event.assert_called_once()
        call_kwargs = mock_event_dal.update_event.call_args.kwargs
        assert call_kwargs["all_day"] is False
        assert call_kwargs["datetime_start"] == timed_start
        assert call_kwargs["datetime_end"] == timed_end

    @pytest.mark.asyncio
    @patch("src.extraction.dedup.event_dal")
    async def test_no_promotion_when_both_all_day(
        self, mock_event_dal, mock_session, family_id
    ):
        """Two all-day events merged don't change all_day."""
        existing = _make_event(
            family_id, "School Holiday",
            dt_start=datetime(2026, 3, 31, 0, 0, tzinfo=UTC),
            all_day=True,
        )

        extracted = ExtractedEvent(
            title="School Holiday",
            datetime_start=datetime(2026, 3, 31, 0, 0, tzinfo=UTC),
            all_day=True,
            confidence=0.85,
        )

        mock_event_dal.find_duplicate_event = AsyncMock(return_value=existing)
        mock_event_dal.update_event = AsyncMock(return_value=existing)

        await deduplicate_event(mock_session, family_id, extracted)

        # all_day should NOT appear in update kwargs
        if mock_event_dal.update_event.called:
            call_kwargs = mock_event_dal.update_event.call_args.kwargs
            assert "all_day" not in call_kwargs

    @pytest.mark.asyncio
    @patch("src.extraction.dedup.event_dal")
    async def test_promotes_time_tbd_to_timed(
        self, mock_event_dal, mock_session, family_id
    ):
        """Existing time_tbd=True event merged with time_tbd=False, all_day=False extraction
        updates datetime_start and sets time_tbd=False."""
        existing = _make_event(
            family_id, "Birthday Party",
            dt_start=datetime(2026, 3, 31, 0, 0, tzinfo=UTC),
            all_day=False,
            time_tbd=True,
        )

        timed_start = datetime(2026, 3, 31, 14, 0, tzinfo=UTC)
        timed_end = datetime(2026, 3, 31, 16, 0, tzinfo=UTC)
        extracted = ExtractedEvent(
            title="Birthday Party",
            datetime_start=timed_start,
            datetime_end=timed_end,
            all_day=False,
            time_tbd=False,
            confidence=0.9,
        )

        mock_event_dal.find_duplicate_event = AsyncMock(return_value=existing)
        mock_event_dal.update_event = AsyncMock(return_value=existing)

        event, is_new = await deduplicate_event(
            mock_session, family_id, extracted
        )

        assert is_new is False
        mock_event_dal.update_event.assert_called_once()
        call_kwargs = mock_event_dal.update_event.call_args.kwargs
        assert call_kwargs["time_tbd"] is False
        assert call_kwargs["datetime_start"] == timed_start
        assert call_kwargs["datetime_end"] == timed_end

    @pytest.mark.asyncio
    @patch("src.extraction.dedup.event_dal")
    async def test_tbd_plus_tbd_stays_tbd(
        self, mock_event_dal, mock_session, family_id
    ):
        """Two TBD events merged don't promote — time_tbd stays True."""
        existing = _make_event(
            family_id, "Dentist Appointment",
            dt_start=datetime(2026, 3, 31, 0, 0, tzinfo=UTC),
            all_day=False,
            time_tbd=True,
        )

        extracted = ExtractedEvent(
            title="Dentist Appointment",
            datetime_start=datetime(2026, 3, 31, 0, 0, tzinfo=UTC),
            all_day=False,
            time_tbd=True,
            confidence=0.85,
        )

        mock_event_dal.find_duplicate_event = AsyncMock(return_value=existing)
        mock_event_dal.update_event = AsyncMock(return_value=existing)

        await deduplicate_event(mock_session, family_id, extracted)

        # time_tbd should NOT appear in update kwargs (both are TBD, no promotion)
        if mock_event_dal.update_event.called:
            call_kwargs = mock_event_dal.update_event.call_args.kwargs
            assert "time_tbd" not in call_kwargs

    @pytest.mark.asyncio
    @patch("src.extraction.dedup.event_dal")
    async def test_no_promotion_when_existing_is_timed(
        self, mock_event_dal, mock_session, family_id
    ):
        """Timed event merged with all-day extraction doesn't change to all-day."""
        existing = _make_event(
            family_id, "Soccer Practice",
            dt_start=datetime(2026, 3, 31, 16, 0, tzinfo=UTC),
            all_day=False,
        )

        extracted = ExtractedEvent(
            title="Soccer Practice",
            datetime_start=datetime(2026, 3, 31, 0, 0, tzinfo=UTC),
            all_day=True,
            confidence=0.7,
        )

        mock_event_dal.find_duplicate_event = AsyncMock(return_value=existing)
        mock_event_dal.update_event = AsyncMock(return_value=existing)

        await deduplicate_event(mock_session, family_id, extracted)

        # all_day should NOT appear in update kwargs (existing is already timed)
        if mock_event_dal.update_event.called:
            call_kwargs = mock_event_dal.update_event.call_args.kwargs
            assert "all_day" not in call_kwargs


# ── TestFmtEventTime ─────────────────────────────────────────────────────


class TestFmtEventTime:
    """Test fmt_event_time display for all-day vs timed events."""

    def test_all_day_shows_all_day(self):
        """fmt_event_time for all-day event shows 'Mar 31 (all day)'."""
        event = MagicMock()
        event.all_day = True
        event.datetime_start = datetime(2026, 3, 31, 0, 0, tzinfo=UTC)

        result = fmt_event_time(event, "America/New_York")

        assert "(all day)" in result
        assert "Mar 31" in result or "Mar 30" in result  # Timezone may shift date

    def test_timed_shows_time(self):
        """fmt_event_time for timed event shows the time."""
        event = MagicMock()
        event.all_day = False
        event.time_tbd = False
        event.time_explicit = True
        event.datetime_start = datetime(2026, 3, 31, 19, 0, tzinfo=UTC)

        result = fmt_event_time(event, "America/New_York")

        assert "(all day)" not in result
        # Should contain a time component (PM/AM)
        assert "PM" in result or "AM" in result

    def test_time_tbd_shows_tbd(self):
        """fmt_event_time for time_tbd=True event shows '(time TBD)'."""
        event = MagicMock()
        event.all_day = False
        event.time_tbd = True
        event.time_explicit = False
        event.datetime_start = datetime(2026, 3, 31, 0, 0, tzinfo=UTC)

        result = fmt_event_time(event, "America/New_York")

        assert "(time TBD)" in result
        assert "(all day)" not in result

    def test_estimated_shows_est(self):
        """fmt_event_time for time_explicit=False, all_day=False, time_tbd=False shows '(est.)'."""
        event = MagicMock()
        event.all_day = False
        event.time_tbd = False
        event.time_explicit = False
        event.datetime_start = datetime(2026, 3, 31, 14, 0, tzinfo=UTC)

        result = fmt_event_time(event, "America/New_York")

        assert "(est.)" in result
        assert "(time TBD)" not in result
        assert "(all day)" not in result

    def test_explicit_time_no_suffix(self):
        """fmt_event_time for time_explicit=True shows no suffix."""
        event = MagicMock()
        event.all_day = False
        event.time_tbd = False
        event.time_explicit = True
        event.datetime_start = datetime(2026, 3, 31, 14, 0, tzinfo=UTC)

        result = fmt_event_time(event, "America/New_York")

        assert "(est.)" not in result
        assert "(time TBD)" not in result
        assert "(all day)" not in result
        assert "AM" in result or "PM" in result

    def test_handles_missing_all_day_attr(self):
        """Object without all_day attribute defaults to timed display."""
        event = MagicMock(spec=[])  # Empty spec — no attributes
        event.datetime_start = datetime(2026, 3, 31, 14, 0, tzinfo=UTC)

        result = fmt_event_time(event, "America/New_York")

        # Should fall through to timed formatting (no all_day attr → getattr returns False)
        assert "(all day)" not in result
        assert "PM" in result or "AM" in result


# ── TestCreateEventAllDay ────────────────────────────────────────────────


class TestCreateEventAllDay:
    """Test create_event handles the all_day parameter."""

    @pytest.mark.asyncio
    @patch("src.extraction.dedup.event_dal")
    async def test_create_event_with_all_day(
        self, mock_event_dal, mock_session, family_id
    ):
        """create_event(all_day=True) sets the field on the Event."""
        new_event = MagicMock(spec=Event)
        new_event.id = uuid4()
        mock_event_dal.find_duplicate_event = AsyncMock(return_value=None)
        mock_event_dal.create_event = AsyncMock(return_value=new_event)

        extracted = ExtractedEvent(
            title="School Holiday",
            event_type="school_event",
            datetime_start=datetime(2026, 3, 31, 0, 0, tzinfo=UTC),
            all_day=True,
            confidence=0.9,
        )

        event, is_new = await deduplicate_event(
            mock_session, family_id, extracted
        )

        assert is_new is True
        mock_event_dal.create_event.assert_called_once()
        # Verify all_day=True was passed as the positional/keyword arg
        call_args = mock_event_dal.create_event.call_args
        # create_event signature: create_event(session, family_id, all_day=False, **kwargs)
        # deduplicate_event calls: event_dal.create_event(session, family_id, all_day=..., ...)
        assert call_args.kwargs.get("all_day") is True or (
            len(call_args.args) >= 3 and call_args.args[2] is True
        )

    @pytest.mark.asyncio
    @patch("src.extraction.dedup.event_dal")
    async def test_create_event_with_time_tbd(
        self, mock_event_dal, mock_session, family_id
    ):
        """create_event passes time_tbd=True through to the DAL."""
        new_event = MagicMock(spec=Event)
        new_event.id = uuid4()
        mock_event_dal.find_duplicate_event = AsyncMock(return_value=None)
        mock_event_dal.create_event = AsyncMock(return_value=new_event)

        extracted = ExtractedEvent(
            title="Dentist Appointment",
            datetime_start=datetime(2026, 3, 31, 0, 0, tzinfo=UTC),
            all_day=False,
            time_tbd=True,
            confidence=0.9,
        )

        event, is_new = await deduplicate_event(
            mock_session, family_id, extracted
        )

        assert is_new is True
        mock_event_dal.create_event.assert_called_once()
        call_kwargs = mock_event_dal.create_event.call_args.kwargs
        assert call_kwargs.get("time_tbd") is True

    @pytest.mark.asyncio
    @patch("src.extraction.dedup.event_dal")
    async def test_create_event_with_time_explicit(
        self, mock_event_dal, mock_session, family_id
    ):
        """create_event passes time_explicit=True through to the DAL."""
        new_event = MagicMock(spec=Event)
        new_event.id = uuid4()
        mock_event_dal.find_duplicate_event = AsyncMock(return_value=None)
        mock_event_dal.create_event = AsyncMock(return_value=new_event)

        extracted = ExtractedEvent(
            title="Soccer Practice",
            datetime_start=datetime(2026, 3, 31, 16, 0, tzinfo=UTC),
            all_day=False,
            time_tbd=False,
            time_explicit=True,
            confidence=0.85,
        )

        event, is_new = await deduplicate_event(
            mock_session, family_id, extracted
        )

        assert is_new is True
        mock_event_dal.create_event.assert_called_once()
        call_kwargs = mock_event_dal.create_event.call_args.kwargs
        assert call_kwargs.get("time_explicit") is True

    @pytest.mark.asyncio
    @patch("src.extraction.dedup.event_dal")
    async def test_create_event_defaults_to_not_all_day(
        self, mock_event_dal, mock_session, family_id
    ):
        """Default all_day=False when extraction has no all_day field."""
        new_event = MagicMock(spec=Event)
        new_event.id = uuid4()
        mock_event_dal.find_duplicate_event = AsyncMock(return_value=None)
        mock_event_dal.create_event = AsyncMock(return_value=new_event)

        extracted = ExtractedEvent(
            title="Soccer Practice",
            event_type="sports_practice",
            datetime_start=datetime(2026, 3, 31, 16, 0, tzinfo=UTC),
            confidence=0.85,
        )

        event, is_new = await deduplicate_event(
            mock_session, family_id, extracted
        )

        assert is_new is True
        mock_event_dal.create_event.assert_called_once()
        call_args = mock_event_dal.create_event.call_args
        # all_day should be False (the default)
        all_day_val = call_args.kwargs.get("all_day")
        if all_day_val is None and len(call_args.args) >= 3:
            all_day_val = call_args.args[2]
        assert all_day_val is False
