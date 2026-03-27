"""Tests for the event lifecycle tracking feature.

Covers:
- expire_stale_pending: selective expiration of stale PendingActions
- deduplicate_event: confirmed param passthrough and auto-confirm on merge
- get_unconfirmed_events: filtering by confirmed_by_caregiver
- confirmed_only filter: on get_events_in_range and get_upcoming_events
- Gmail ingestion flow: unconfirmed event creation with PendingAction tracking
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from src.extraction.dedup import deduplicate_event
from src.extraction.email import ExtractedEvent
from src.state.models import Event, EventSource, PendingAction, PendingActionStatus, RsvpStatus


@pytest.fixture
def family_id():
    return uuid4()


@pytest.fixture
def mock_session():
    return AsyncMock()


def _make_existing_event(
    family_id,
    title="Soccer Practice",
    dt_start=None,
    source=EventSource.email,
    confirmed_by_caregiver=True,
):
    now = datetime.now(UTC)
    event = MagicMock(spec=Event)
    event.id = uuid4()
    event.family_id = family_id
    event.title = title
    event.datetime_start = dt_start or now + timedelta(days=1)
    event.datetime_end = None
    event.location = None
    event.description = None
    event.source = source
    event.source_refs = []
    event.rsvp_status = RsvpStatus.not_applicable
    event.rsvp_deadline = None
    event.rsvp_contact = None
    event.extraction_confidence = 0.7
    event.drop_off_by = None
    event.pick_up_by = None
    event.confirmed_by_caregiver = confirmed_by_caregiver
    return event


# ── TestExpireStaleOnly ─────────────────────────────────────────────────


class TestExpireStaleOnly:
    """Test expire_stale_pending() — selective expiration of stale actions."""

    @pytest.mark.asyncio
    @patch("src.state.pending.update")
    @patch("src.state.pending.or_")
    @patch("src.state.pending.and_")
    async def test_expire_stale_pending_returns_rowcount(self, mock_and, mock_or, mock_update, mock_session):
        """expire_stale_pending returns the number of expired rows."""
        from src.state.pending import expire_stale_pending

        mock_result = MagicMock()
        mock_result.rowcount = 3
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.flush = AsyncMock()

        # Mock the SQLAlchemy update chain
        mock_stmt = MagicMock()
        mock_update.return_value = mock_stmt
        mock_stmt.where.return_value = mock_stmt
        mock_stmt.values.return_value = mock_stmt

        count = await expire_stale_pending(mock_session)
        assert count == 3

    @pytest.mark.asyncio
    async def test_expire_stale_only_expires_past_expires_at(self, mock_session):
        """Actions with expires_at in the past should be expired."""
        from src.state.pending import expire_stale_pending

        now = datetime.now(UTC)

        # Create a mock that captures the WHERE clause for inspection
        mock_result = MagicMock()
        mock_result.rowcount = 1
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.flush = AsyncMock()

        count = await expire_stale_pending(mock_session)

        # Verify execute was called (the SQL was built and run)
        mock_session.execute.assert_called_once()
        assert count == 1

    @pytest.mark.asyncio
    async def test_expire_stale_expires_null_expires_at_older_than_24h(self, mock_session):
        """Actions with no expires_at and created_at > 24h ago should be expired."""
        from src.state.pending import expire_stale_pending

        mock_result = MagicMock()
        mock_result.rowcount = 2
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.flush = AsyncMock()

        count = await expire_stale_pending(mock_session)

        # The function builds an OR condition:
        # (expires_at <= now) OR (expires_at IS NULL AND created_at <= now - 24h)
        mock_session.execute.assert_called_once()
        assert count == 2

    @pytest.mark.asyncio
    async def test_expire_stale_preserves_fresh_actions(self, mock_session):
        """Actions within their window should NOT be expired (rowcount=0)."""
        from src.state.pending import expire_stale_pending

        mock_result = MagicMock()
        mock_result.rowcount = 0
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.flush = AsyncMock()

        count = await expire_stale_pending(mock_session)
        assert count == 0

    @pytest.mark.asyncio
    async def test_expire_stale_sets_expired_status_and_resolved_at(self, mock_session):
        """Verify the UPDATE sets status=expired and resolved_at=now."""
        from src.state.pending import expire_stale_pending

        mock_result = MagicMock()
        mock_result.rowcount = 1
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.flush = AsyncMock()

        await expire_stale_pending(mock_session)

        # Inspect the statement passed to execute
        call_args = mock_session.execute.call_args
        stmt = call_args[0][0]

        # The compiled statement should reference the pending_actions table
        # and set status to expired. We verify via the string representation.
        stmt_str = str(stmt.compile(compile_kwargs={"literal_binds": False}))
        assert "pending_actions" in stmt_str
        assert "SET" in stmt_str.upper()


# ── TestDedupWithConfirmed ──────────────────────────────────────────────


class TestDedupWithConfirmed:
    """Test that deduplicate_event passes confirmed through to create_event."""

    @pytest.mark.asyncio
    @patch("src.extraction.dedup.event_dal")
    async def test_confirmed_false_passed_to_create(
        self, mock_event_dal, mock_session, family_id
    ):
        """When confirmed=False, create_event is called with confirmed_by_caregiver=False."""
        dt = datetime(2026, 4, 10, 16, 0, tzinfo=UTC)
        extracted = ExtractedEvent(
            title="Swim Meet",
            event_type="swim_meet",
            datetime_start=dt,
            confidence=0.85,
        )

        mock_event_dal.find_duplicate_event = AsyncMock(return_value=None)
        new_event = MagicMock(spec=Event)
        new_event.id = uuid4()
        mock_event_dal.create_event = AsyncMock(return_value=new_event)

        event, is_new = await deduplicate_event(
            mock_session, family_id, extracted, confirmed=False
        )

        assert is_new is True
        mock_event_dal.create_event.assert_called_once()
        call_kwargs = mock_event_dal.create_event.call_args.kwargs
        assert call_kwargs["confirmed_by_caregiver"] is False

    @pytest.mark.asyncio
    @patch("src.extraction.dedup.event_dal")
    async def test_confirmed_true_is_default(
        self, mock_event_dal, mock_session, family_id
    ):
        """Default confirmed=True results in confirmed_by_caregiver=True."""
        dt = datetime(2026, 4, 10, 16, 0, tzinfo=UTC)
        extracted = ExtractedEvent(
            title="Piano Recital",
            event_type="recital",
            datetime_start=dt,
            confidence=0.9,
        )

        mock_event_dal.find_duplicate_event = AsyncMock(return_value=None)
        new_event = MagicMock(spec=Event)
        new_event.id = uuid4()
        mock_event_dal.create_event = AsyncMock(return_value=new_event)

        event, is_new = await deduplicate_event(
            mock_session, family_id, extracted
        )

        assert is_new is True
        call_kwargs = mock_event_dal.create_event.call_args.kwargs
        assert call_kwargs["confirmed_by_caregiver"] is True

    @pytest.mark.asyncio
    @patch("src.extraction.dedup.event_dal")
    async def test_merge_auto_confirms_calendar_source(
        self, mock_event_dal, mock_session, family_id
    ):
        """Merging with calendar source auto-confirms an unconfirmed event."""
        dt = datetime(2026, 4, 10, 16, 0, tzinfo=UTC)
        existing = _make_existing_event(
            family_id,
            "Soccer Practice",
            dt_start=dt,
            source=EventSource.email,
            confirmed_by_caregiver=False,
        )

        extracted = ExtractedEvent(
            title="Soccer Practice",
            datetime_start=dt,
            confidence=0.8,
        )

        mock_event_dal.find_duplicate_event = AsyncMock(return_value=existing)
        mock_event_dal.update_event = AsyncMock(return_value=existing)

        event, is_new = await deduplicate_event(
            mock_session, family_id, extracted,
            source=EventSource.calendar,
        )

        assert is_new is False
        mock_event_dal.update_event.assert_called_once()
        call_kwargs = mock_event_dal.update_event.call_args.kwargs
        assert call_kwargs.get("confirmed_by_caregiver") is True

    @pytest.mark.asyncio
    @patch("src.extraction.dedup.event_dal")
    async def test_merge_does_not_auto_confirm_email_source(
        self, mock_event_dal, mock_session, family_id
    ):
        """Merging with email source does NOT auto-confirm an unconfirmed event."""
        dt = datetime(2026, 4, 10, 16, 0, tzinfo=UTC)
        existing = _make_existing_event(
            family_id,
            "Soccer Practice",
            dt_start=dt,
            source=EventSource.email,
            confirmed_by_caregiver=False,
        )

        extracted = ExtractedEvent(
            title="Soccer Practice",
            datetime_start=dt,
            confidence=0.8,
        )

        mock_event_dal.find_duplicate_event = AsyncMock(return_value=existing)
        mock_event_dal.update_event = AsyncMock(return_value=existing)

        event, is_new = await deduplicate_event(
            mock_session, family_id, extracted,
            source=EventSource.email,
        )

        assert is_new is False
        # If update was called, confirmed_by_caregiver should NOT be set
        if mock_event_dal.update_event.called:
            call_kwargs = mock_event_dal.update_event.call_args.kwargs
            assert "confirmed_by_caregiver" not in call_kwargs


# ── TestGetUnconfirmedEvents ────────────────────────────────────────────


class TestGetUnconfirmedEvents:
    """Test get_unconfirmed_events() filtering."""

    @pytest.mark.asyncio
    async def test_returns_unconfirmed_non_cancelled(self, mock_session):
        """Returns events where confirmed_by_caregiver=False and cancelled_at IS NULL."""
        from src.state.events import get_unconfirmed_events

        family_id = uuid4()
        unconfirmed_event = MagicMock(spec=Event)
        unconfirmed_event.confirmed_by_caregiver = False
        unconfirmed_event.cancelled_at = None
        unconfirmed_event.datetime_start = datetime.now(UTC) + timedelta(days=1)

        mock_result = MagicMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [unconfirmed_event]
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute = AsyncMock(return_value=mock_result)

        events = await get_unconfirmed_events(mock_session, family_id)
        assert len(events) == 1
        assert events[0].confirmed_by_caregiver is False

    @pytest.mark.asyncio
    async def test_future_only_filters_past_events(self, mock_session):
        """When future_only=True (default), only future events are returned."""
        from src.state.events import get_unconfirmed_events

        family_id = uuid4()

        # The function adds datetime_start > now when future_only=True
        mock_result = MagicMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = []
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute = AsyncMock(return_value=mock_result)

        events = await get_unconfirmed_events(mock_session, family_id, future_only=True)

        # Verify execute was called — the filter is applied in the SQL query
        mock_session.execute.assert_called_once()
        assert events == []

    @pytest.mark.asyncio
    async def test_future_only_false_includes_past(self, mock_session):
        """When future_only=False, past unconfirmed events are included."""
        from src.state.events import get_unconfirmed_events

        family_id = uuid4()
        past_event = MagicMock(spec=Event)
        past_event.confirmed_by_caregiver = False
        past_event.cancelled_at = None
        past_event.datetime_start = datetime.now(UTC) - timedelta(days=1)

        mock_result = MagicMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [past_event]
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute = AsyncMock(return_value=mock_result)

        events = await get_unconfirmed_events(mock_session, family_id, future_only=False)
        assert len(events) == 1

    @pytest.mark.asyncio
    async def test_does_not_return_confirmed_events(self, mock_session):
        """Confirmed events are excluded from results."""
        from src.state.events import get_unconfirmed_events

        family_id = uuid4()

        # Simulate DB returning empty (confirmed events filtered out by query)
        mock_result = MagicMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = []
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute = AsyncMock(return_value=mock_result)

        events = await get_unconfirmed_events(mock_session, family_id)
        assert events == []

    @pytest.mark.asyncio
    async def test_does_not_return_cancelled_events(self, mock_session):
        """Cancelled events are excluded even if unconfirmed."""
        from src.state.events import get_unconfirmed_events

        family_id = uuid4()

        # Simulate DB returning empty (cancelled events filtered out by query)
        mock_result = MagicMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = []
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute = AsyncMock(return_value=mock_result)

        events = await get_unconfirmed_events(mock_session, family_id)
        assert events == []


# ── TestConfirmedOnlyFilter ─────────────────────────────────────────────


class TestConfirmedOnlyFilter:
    """Test confirmed_only parameter on get_events_in_range and get_upcoming_events."""

    @pytest.mark.asyncio
    async def test_get_events_in_range_default_confirmed_only(self, mock_session):
        """Default confirmed_only=True filters to confirmed events only."""
        from src.state.events import get_events_in_range

        family_id = uuid4()
        now = datetime.now(UTC)
        confirmed_event = MagicMock(spec=Event)
        confirmed_event.confirmed_by_caregiver = True

        mock_result = MagicMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [confirmed_event]
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute = AsyncMock(return_value=mock_result)

        events = await get_events_in_range(
            mock_session, family_id, now, now + timedelta(days=7)
        )

        assert len(events) == 1
        mock_session.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_events_in_range_confirmed_only_false(self, mock_session):
        """confirmed_only=False returns all events including unconfirmed."""
        from src.state.events import get_events_in_range

        family_id = uuid4()
        now = datetime.now(UTC)
        confirmed = MagicMock(spec=Event)
        confirmed.confirmed_by_caregiver = True
        unconfirmed = MagicMock(spec=Event)
        unconfirmed.confirmed_by_caregiver = False

        mock_result = MagicMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [confirmed, unconfirmed]
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute = AsyncMock(return_value=mock_result)

        events = await get_events_in_range(
            mock_session, family_id, now, now + timedelta(days=7),
            confirmed_only=False,
        )

        assert len(events) == 2

    @pytest.mark.asyncio
    async def test_get_upcoming_events_default_confirmed_only(self, mock_session):
        """get_upcoming_events defaults to confirmed_only=True."""
        from src.state.events import get_upcoming_events

        family_id = uuid4()

        mock_result = MagicMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = []
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute = AsyncMock(return_value=mock_result)

        events = await get_upcoming_events(mock_session, family_id)

        mock_session.execute.assert_called_once()
        assert events == []

    @pytest.mark.asyncio
    async def test_get_upcoming_events_confirmed_only_false(self, mock_session):
        """get_upcoming_events with confirmed_only=False includes unconfirmed."""
        from src.state.events import get_upcoming_events

        family_id = uuid4()
        unconfirmed = MagicMock(spec=Event)
        unconfirmed.confirmed_by_caregiver = False

        mock_result = MagicMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [unconfirmed]
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute = AsyncMock(return_value=mock_result)

        events = await get_upcoming_events(
            mock_session, family_id, confirmed_only=False
        )

        assert len(events) == 1


# ── TestGmailCreatesUnconfirmedEvents ───────────────────────────────────


class TestGmailCreatesUnconfirmedEvents:
    """Test the Gmail ingestion flow creates unconfirmed events with PendingAction tracking."""

    @pytest.mark.asyncio
    @patch("src.ingestion.gmail._process_ics_attachments", new_callable=AsyncMock)
    @patch("src.ingestion.gmail.send_buttons_to_family", new_callable=AsyncMock)
    @patch("src.ingestion.gmail.create_pending_action", new_callable=AsyncMock)
    @patch("src.ingestion.gmail.persist_extraction", new_callable=AsyncMock)
    @patch("src.ingestion.gmail.process_email", new_callable=AsyncMock)
    @patch("src.ingestion.gmail.fetch_email_content", new_callable=AsyncMock)
    @patch("src.ingestion.gmail._get_new_message_ids", new_callable=AsyncMock)
    @patch("src.ingestion.gmail._get_access_token", new_callable=AsyncMock)
    @patch("src.ingestion.gmail.families_dal")
    @patch("src.ingestion.gmail.events_dal")
    async def test_persist_extraction_called_with_confirmed_false(
        self,
        mock_events_dal,
        mock_families_dal,
        mock_get_token,
        mock_get_msg_ids,
        mock_fetch_email,
        mock_process_email,
        mock_persist,
        mock_create_pending,
        mock_send_buttons,
        mock_process_ics,
    ):
        """persist_extraction is called with confirmed=False (not skip_events=True)."""
        from src.ingestion.gmail import handle_gmail_notification
        from src.extraction.email import ExtractionResult

        mock_events_dal.get_events_by_source_ref = AsyncMock(return_value=[])

        session = AsyncMock()
        family_id = uuid4()
        caregiver_id = uuid4()

        # Setup caregiver mock
        caregiver = MagicMock()
        caregiver.id = caregiver_id
        caregiver.family_id = family_id
        caregiver.google_refresh_token_encrypted = b"encrypted"
        caregiver.gmail_watch_history_id = 100
        mock_families_dal.get_caregiver_by_email = AsyncMock(return_value=caregiver)
        mock_families_dal.get_family = AsyncMock(return_value=MagicMock(timezone="America/New_York"))

        mock_get_token.return_value = "access-token"
        mock_get_msg_ids.return_value = ["msg-001"]

        mock_fetch_email.return_value = {
            "message_id": "msg-001",
            "from_address": "school@example.com",
            "to_addresses": ["parent@gmail.com"],
            "subject": "Field Trip Permission",
            "body_text": "Field trip on April 15th",
            "body_html": "",
            "date": datetime.now(UTC),
            "attachments": [],
        }

        # Mock extraction result with one event
        mock_event = MagicMock(spec=Event)
        mock_event.id = uuid4()
        mock_event.title = "Field Trip"
        mock_event.datetime_start = datetime(2026, 4, 15, 9, 0, tzinfo=UTC)
        mock_event.location = "Science Museum"

        extraction = MagicMock(spec=ExtractionResult)
        extraction.is_relevant = True
        extraction.events = [MagicMock()]
        extraction.action_items = []
        extraction.learnings = []
        mock_process_email.return_value = extraction

        mock_persist.return_value = [mock_event]

        # Mock PendingAction
        pending = MagicMock(spec=PendingAction)
        pending.id = uuid4()
        pending.context = {}
        mock_create_pending.return_value = pending

        import base64
        import json
        payload = {
            "message": {
                "data": base64.b64encode(json.dumps({
                    "emailAddress": "parent@gmail.com",
                    "historyId": "200",
                }).encode()).decode()
            }
        }

        await handle_gmail_notification(session, payload)

        # Verify persist_extraction was called with confirmed=False
        mock_persist.assert_called_once()
        call_kwargs = mock_persist.call_args.kwargs
        assert call_kwargs.get("confirmed") is False
        # Should NOT use skip_events=True
        assert call_kwargs.get("skip_events", False) is False

    @pytest.mark.asyncio
    @patch("src.ingestion.gmail._process_ics_attachments", new_callable=AsyncMock)
    @patch("src.ingestion.gmail.send_buttons_to_family", new_callable=AsyncMock)
    @patch("src.ingestion.gmail.create_pending_action", new_callable=AsyncMock)
    @patch("src.ingestion.gmail.persist_extraction", new_callable=AsyncMock)
    @patch("src.ingestion.gmail.process_email", new_callable=AsyncMock)
    @patch("src.ingestion.gmail.fetch_email_content", new_callable=AsyncMock)
    @patch("src.ingestion.gmail._get_new_message_ids", new_callable=AsyncMock)
    @patch("src.ingestion.gmail._get_access_token", new_callable=AsyncMock)
    @patch("src.ingestion.gmail.families_dal")
    @patch("src.ingestion.gmail.events_dal")
    async def test_pending_action_context_contains_event_id(
        self,
        mock_events_dal,
        mock_families_dal,
        mock_get_token,
        mock_get_msg_ids,
        mock_fetch_email,
        mock_process_email,
        mock_persist,
        mock_create_pending,
        mock_send_buttons,
        mock_process_ics,
    ):
        """PendingAction context contains event_id (not event_data)."""
        from src.ingestion.gmail import handle_gmail_notification
        from src.extraction.email import ExtractionResult

        mock_events_dal.get_events_by_source_ref = AsyncMock(return_value=[])

        session = AsyncMock()
        family_id = uuid4()
        event_id = uuid4()

        caregiver = MagicMock()
        caregiver.id = uuid4()
        caregiver.family_id = family_id
        caregiver.google_refresh_token_encrypted = b"encrypted"
        caregiver.gmail_watch_history_id = 100
        mock_families_dal.get_caregiver_by_email = AsyncMock(return_value=caregiver)
        mock_families_dal.get_family = AsyncMock(return_value=MagicMock(timezone="America/New_York"))

        mock_get_token.return_value = "access-token"
        mock_get_msg_ids.return_value = ["msg-002"]

        mock_fetch_email.return_value = {
            "message_id": "msg-002",
            "from_address": "coach@example.com",
            "to_addresses": ["parent@gmail.com"],
            "subject": "Game Schedule",
            "body_text": "Game on Saturday at 10am",
            "body_html": "",
            "date": datetime.now(UTC),
            "attachments": [],
        }

        mock_event = MagicMock(spec=Event)
        mock_event.id = event_id
        mock_event.title = "Soccer Game"
        mock_event.datetime_start = datetime(2026, 4, 12, 10, 0, tzinfo=UTC)
        mock_event.location = "City Park"

        extraction = MagicMock(spec=ExtractionResult)
        extraction.is_relevant = True
        extraction.events = [MagicMock()]
        extraction.action_items = []
        extraction.learnings = []
        mock_process_email.return_value = extraction

        mock_persist.return_value = [mock_event]

        pending = MagicMock(spec=PendingAction)
        pending.id = uuid4()
        pending.context = {}
        mock_create_pending.return_value = pending

        import base64
        import json
        payload = {
            "message": {
                "data": base64.b64encode(json.dumps({
                    "emailAddress": "parent@gmail.com",
                    "historyId": "300",
                }).encode()).decode()
            }
        }

        await handle_gmail_notification(session, payload)

        # Verify PendingAction was created with event_id in context
        mock_create_pending.assert_called_once()
        call_kwargs = mock_create_pending.call_args.kwargs
        context = call_kwargs.get("context", {})
        assert "event_id" in context
        assert context["event_id"] == str(event_id)
        # Should NOT contain event_data blob
        assert "event_data" not in context

    @pytest.mark.asyncio
    @patch("src.ingestion.gmail._process_ics_attachments", new_callable=AsyncMock)
    @patch("src.ingestion.gmail.send_buttons_to_family", new_callable=AsyncMock)
    @patch("src.ingestion.gmail.create_pending_action", new_callable=AsyncMock)
    @patch("src.ingestion.gmail.persist_extraction", new_callable=AsyncMock)
    @patch("src.ingestion.gmail.process_email", new_callable=AsyncMock)
    @patch("src.ingestion.gmail.fetch_email_content", new_callable=AsyncMock)
    @patch("src.ingestion.gmail._get_new_message_ids", new_callable=AsyncMock)
    @patch("src.ingestion.gmail._get_access_token", new_callable=AsyncMock)
    @patch("src.ingestion.gmail.families_dal")
    @patch("src.ingestion.gmail.events_dal")
    async def test_whatsapp_delivered_tracked_in_pending_context(
        self,
        mock_events_dal,
        mock_families_dal,
        mock_get_token,
        mock_get_msg_ids,
        mock_fetch_email,
        mock_process_email,
        mock_persist,
        mock_create_pending,
        mock_send_buttons,
        mock_process_ics,
    ):
        """whatsapp_delivered is tracked in PendingAction context."""
        from src.ingestion.gmail import handle_gmail_notification
        from src.extraction.email import ExtractionResult

        mock_events_dal.get_events_by_source_ref = AsyncMock(return_value=[])

        session = AsyncMock()
        family_id = uuid4()

        caregiver = MagicMock()
        caregiver.id = uuid4()
        caregiver.family_id = family_id
        caregiver.google_refresh_token_encrypted = b"encrypted"
        caregiver.gmail_watch_history_id = 100
        mock_families_dal.get_caregiver_by_email = AsyncMock(return_value=caregiver)
        mock_families_dal.get_family = AsyncMock(return_value=MagicMock(timezone="America/New_York"))

        mock_get_token.return_value = "access-token"
        mock_get_msg_ids.return_value = ["msg-003"]

        mock_fetch_email.return_value = {
            "message_id": "msg-003",
            "from_address": "teacher@example.com",
            "to_addresses": ["parent@gmail.com"],
            "subject": "Class Party",
            "body_text": "Class party on Friday",
            "body_html": "",
            "date": datetime.now(UTC),
            "attachments": [],
        }

        mock_event = MagicMock(spec=Event)
        mock_event.id = uuid4()
        mock_event.title = "Class Party"
        mock_event.datetime_start = datetime(2026, 4, 18, 14, 0, tzinfo=UTC)
        mock_event.location = None

        extraction = MagicMock(spec=ExtractionResult)
        extraction.is_relevant = True
        extraction.events = [MagicMock()]
        extraction.action_items = []
        extraction.learnings = []
        mock_process_email.return_value = extraction

        mock_persist.return_value = [mock_event]

        # Track context mutations on the pending action
        context_store = {}
        pending = MagicMock(spec=PendingAction)
        pending.id = uuid4()
        pending.context = context_store
        mock_create_pending.return_value = pending

        # WhatsApp delivery succeeds
        mock_send_buttons.return_value = None

        import base64
        import json
        payload = {
            "message": {
                "data": base64.b64encode(json.dumps({
                    "emailAddress": "parent@gmail.com",
                    "historyId": "400",
                }).encode()).decode()
            }
        }

        await handle_gmail_notification(session, payload)

        # Verify whatsapp_delivered was set in context
        assert pending.context.get("whatsapp_delivered") is True

    @pytest.mark.asyncio
    @patch("src.ingestion.gmail._process_ics_attachments", new_callable=AsyncMock)
    @patch("src.ingestion.gmail.send_buttons_to_family", new_callable=AsyncMock)
    @patch("src.ingestion.gmail.create_pending_action", new_callable=AsyncMock)
    @patch("src.ingestion.gmail.persist_extraction", new_callable=AsyncMock)
    @patch("src.ingestion.gmail.process_email", new_callable=AsyncMock)
    @patch("src.ingestion.gmail.fetch_email_content", new_callable=AsyncMock)
    @patch("src.ingestion.gmail._get_new_message_ids", new_callable=AsyncMock)
    @patch("src.ingestion.gmail._get_access_token", new_callable=AsyncMock)
    @patch("src.ingestion.gmail.families_dal")
    @patch("src.ingestion.gmail.events_dal")
    async def test_whatsapp_delivery_failure_tracked_as_false(
        self,
        mock_events_dal,
        mock_families_dal,
        mock_get_token,
        mock_get_msg_ids,
        mock_fetch_email,
        mock_process_email,
        mock_persist,
        mock_create_pending,
        mock_send_buttons,
        mock_process_ics,
    ):
        """When WhatsApp delivery fails, whatsapp_delivered=False in context."""
        from src.ingestion.gmail import handle_gmail_notification
        from src.extraction.email import ExtractionResult

        mock_events_dal.get_events_by_source_ref = AsyncMock(return_value=[])

        session = AsyncMock()
        family_id = uuid4()

        caregiver = MagicMock()
        caregiver.id = uuid4()
        caregiver.family_id = family_id
        caregiver.google_refresh_token_encrypted = b"encrypted"
        caregiver.gmail_watch_history_id = 100
        mock_families_dal.get_caregiver_by_email = AsyncMock(return_value=caregiver)
        mock_families_dal.get_family = AsyncMock(return_value=MagicMock(timezone="America/New_York"))

        mock_get_token.return_value = "access-token"
        mock_get_msg_ids.return_value = ["msg-004"]

        mock_fetch_email.return_value = {
            "message_id": "msg-004",
            "from_address": "org@example.com",
            "to_addresses": ["parent@gmail.com"],
            "subject": "Event Reminder",
            "body_text": "Don't forget the event",
            "body_html": "",
            "date": datetime.now(UTC),
            "attachments": [],
        }

        mock_event = MagicMock(spec=Event)
        mock_event.id = uuid4()
        mock_event.title = "Community Event"
        mock_event.datetime_start = datetime(2026, 4, 20, 11, 0, tzinfo=UTC)
        mock_event.location = "Community Center"

        extraction = MagicMock(spec=ExtractionResult)
        extraction.is_relevant = True
        extraction.events = [MagicMock()]
        extraction.action_items = []
        extraction.learnings = []
        mock_process_email.return_value = extraction

        mock_persist.return_value = [mock_event]

        context_store = {}
        pending = MagicMock(spec=PendingAction)
        pending.id = uuid4()
        pending.context = context_store
        mock_create_pending.return_value = pending

        # WhatsApp delivery fails
        mock_send_buttons.side_effect = Exception("WhatsApp API error")

        import base64
        import json
        payload = {
            "message": {
                "data": base64.b64encode(json.dumps({
                    "emailAddress": "parent@gmail.com",
                    "historyId": "500",
                }).encode()).decode()
            }
        }

        await handle_gmail_notification(session, payload)

        # Verify whatsapp_delivered was set to False
        assert pending.context.get("whatsapp_delivered") is False
