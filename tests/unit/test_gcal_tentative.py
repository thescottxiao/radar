"""Tests for tentative GCal event handling.

Covers:
- GCal body conversion: unconfirmed events get tentative status + transparent
- Schedule queries: confirmed_only=False, pending confirmation marker
- Reconciler import: GCal imports are created with confirmed_by_caregiver=True
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from src.actions.gcal import event_to_gcal_body
from src.extraction.schemas import IntentResult, IntentType
from src.state.models import Event, EventSource, RsvpStatus


@pytest.fixture
def family_id():
    return uuid4()


@pytest.fixture
def mock_session():
    return AsyncMock()


def _make_event(
    family_id,
    title="Piano Recital",
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
    event.datetime_start = dt_start or datetime(2026, 4, 15, 18, 0, tzinfo=UTC)
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
    event.cancelled_at = None
    return event


# ── TestGCalBodyTentative ─────────────────────────────────────────────────


class TestGCalBodyTentative:
    """Test event_to_gcal_body tentative status for unconfirmed events."""

    def test_unconfirmed_event_has_pending_prefix(self, family_id):
        """Event with confirmed_by_caregiver=False gets [Pending] prefix
        and transparency='transparent'."""
        event = _make_event(
            family_id,
            title="Birthday Party",
            dt_start=datetime(2026, 4, 15, 14, 0, tzinfo=UTC),
            confirmed_by_caregiver=False,
        )

        body = event_to_gcal_body(event)

        assert body["summary"] == "[Pending] Birthday Party"
        assert body["transparency"] == "transparent"

    def test_confirmed_event_no_pending_prefix(self, family_id):
        """Event with confirmed_by_caregiver=True does NOT have [Pending] prefix."""
        event = _make_event(
            family_id,
            title="Soccer Practice",
            dt_start=datetime(2026, 4, 15, 16, 0, tzinfo=UTC),
            confirmed_by_caregiver=True,
        )

        body = event_to_gcal_body(event)

        assert body["summary"] == "Soccer Practice"
        assert "transparency" not in body

    def test_unconfirmed_allday_has_pending_prefix(self, family_id):
        """All-day + unconfirmed → date format AND [Pending] prefix."""
        event = _make_event(
            family_id,
            title="School Holiday",
            dt_start=datetime(2026, 4, 15, 0, 0, tzinfo=UTC),
            all_day=True,
            confirmed_by_caregiver=False,
        )

        body = event_to_gcal_body(event)

        assert "date" in body["start"]
        assert body["summary"] == "[Pending] School Holiday"
        assert body["transparency"] == "transparent"


# ── TestScheduleShowsPending ──────────────────────────────────────────────


class TestScheduleShowsPending:
    """Test schedule queries include unconfirmed events with pending marker."""

    @pytest.mark.asyncio
    @patch("src.state.families.get_family", new_callable=AsyncMock)
    @patch("src.state.families.get_caregivers_for_family", new_callable=AsyncMock)
    @patch("src.state.children.get_children_for_family", new_callable=AsyncMock)
    @patch("src.extraction.router.events_dal")
    async def test_schedule_query_passes_confirmed_only_false(
        self, mock_events_dal, mock_children_dal, mock_families_dal,
        mock_get_family, mock_session, family_id,
    ):
        """get_upcoming_events is called with confirmed_only=False."""
        from src.extraction.router import _handle_query_schedule

        mock_events_dal.get_upcoming_events = AsyncMock(return_value=[])
        mock_families_dal.return_value = []
        mock_get_family.return_value = MagicMock(timezone="America/New_York")
        mock_children_dal.return_value = []

        intent = IntentResult(
            intent=IntentType.query_schedule,
            confidence=0.95,
            extracted_params={"days": 7},
        )

        await _handle_query_schedule(
            session=mock_session,
            family_id=family_id,
            intent=intent,
            message="What's on this week?",
            sender_id=uuid4(),
        )

        mock_events_dal.get_upcoming_events.assert_called_once()
        call_kwargs = mock_events_dal.get_upcoming_events.call_args
        assert call_kwargs.kwargs.get("confirmed_only") is False

    @pytest.mark.asyncio
    @patch("src.llm.generate", new_callable=AsyncMock)
    @patch("src.state.families.get_family", new_callable=AsyncMock)
    @patch("src.state.families.get_caregivers_for_family", new_callable=AsyncMock)
    @patch("src.state.children.get_children_for_family", new_callable=AsyncMock)
    @patch("src.extraction.router.events_dal")
    async def test_unconfirmed_event_shows_pending_marker(
        self, mock_events_dal, mock_children_dal, mock_families_dal,
        mock_get_family, mock_generate, mock_session, family_id,
    ):
        """Event with confirmed_by_caregiver=False gets '(pending confirmation)' in display."""
        from src.extraction.router import _handle_query_schedule

        unconfirmed_event = _make_event(
            family_id,
            title="Art Class",
            dt_start=datetime(2026, 4, 16, 10, 0, tzinfo=UTC),
            confirmed_by_caregiver=False,
        )

        mock_events_dal.get_upcoming_events = AsyncMock(return_value=[unconfirmed_event])
        mock_families_dal.return_value = []
        mock_get_family.return_value = MagicMock(timezone="America/New_York")
        mock_children_dal.return_value = []

        # The LLM generate call returns a formatted schedule string
        mock_generate.return_value = "Here's your schedule with Art Class"

        intent = IntentResult(
            intent=IntentType.query_schedule,
            confidence=0.95,
            extracted_params={"days": 7},
        )

        result = await _handle_query_schedule(
            session=mock_session,
            family_id=family_id,
            intent=intent,
            message="What's on this week?",
            sender_id=uuid4(),
        )

        # The LLM prompt should include the pending confirmation marker
        mock_generate.assert_called_once()
        prompt_arg = mock_generate.call_args.args[0]
        assert "(pending confirmation)" in prompt_arg


# ── TestReconcilerImportConfirmed ─────────────────────────────────────────


class TestReconcilerImportConfirmed:
    """Test that GCal reconciler imports events as confirmed."""

    @pytest.mark.asyncio
    @patch("src.actions.gcal.list_upcoming_events_from_gcal")
    @patch("src.state.events.get_events_in_range")
    @patch("src.state.events.get_events_by_source_ref")
    @patch("src.state.events.create_event")
    async def test_gcal_import_sets_confirmed(
        self, mock_create_event, mock_get_by_ref, mock_get_range,
        mock_list_gcal, mock_session, family_id,
    ):
        """Verify create_event is called with confirmed_by_caregiver=True
        during reconciler import of a new GCal event."""
        from src.actions.gcal_reconciler import reconcile_family

        # No local events
        mock_get_range.return_value = []

        # One GCal event with no local match
        gcal_event = {
            "title": "Team Meeting",
            "start": "2026-04-20T09:00:00+00:00",
            "end": "2026-04-20T10:00:00+00:00",
            "gcal_id": "gcal_abc123",
            "location": "Office",
            "description": "Weekly sync",
        }
        mock_list_gcal.return_value = [gcal_event]

        # No existing event with this source ref
        mock_get_by_ref.return_value = []

        new_event = MagicMock(spec=Event)
        new_event.id = uuid4()
        mock_create_event.return_value = new_event

        stats = await reconcile_family(mock_session, family_id)

        assert stats["created"] == 1
        mock_create_event.assert_called_once()
        call_kwargs = mock_create_event.call_args.kwargs
        assert call_kwargs["confirmed_by_caregiver"] is True
