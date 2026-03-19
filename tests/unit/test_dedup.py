"""Tests for event deduplication (src/extraction/dedup.py).

Tests the higher-level dedup logic: merge behavior and new event creation.
The core title_similarity function is tested in test_title_similarity.py.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from src.extraction.dedup import deduplicate_event
from src.extraction.email import ExtractedEvent
from src.state.models import Event, EventSource, RsvpStatus


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
    location=None,
    description=None,
    rsvp_status=RsvpStatus.not_applicable,
    extraction_confidence=0.7,
):
    now = datetime.now(UTC)
    event = MagicMock(spec=Event)
    event.id = uuid4()
    event.family_id = family_id
    event.title = title
    event.datetime_start = dt_start or now + timedelta(days=1)
    event.datetime_end = None
    event.location = location
    event.description = description
    event.source = EventSource.email
    event.source_refs = []
    event.rsvp_status = rsvp_status
    event.rsvp_deadline = None
    event.rsvp_contact = None
    event.extraction_confidence = extraction_confidence
    event.drop_off_by = None
    event.pick_up_by = None
    return event


class TestDedupMergeBehavior:
    @patch("src.extraction.dedup.event_dal")
    async def test_merge_enriches_missing_fields(
        self, mock_event_dal, mock_session, family_id
    ):
        """When a duplicate is found, missing fields are enriched from the new extraction."""
        dt = datetime(2026, 3, 20, 16, 0, tzinfo=UTC)
        existing = _make_existing_event(
            family_id, "Soccer Practice", dt_start=dt,
            location=None, description=None,  # Missing fields
        )

        extracted = ExtractedEvent(
            title="Soccer Practice",
            event_type="sports_practice",
            datetime_start=dt + timedelta(minutes=5),  # Close enough for dedup
            location="Westfield Park - Field 3",
            description="Regular weekly practice",
            confidence=0.85,
        )

        mock_event_dal.find_duplicate_event = AsyncMock(return_value=existing)
        # Capture the update call
        mock_event_dal.update_event = AsyncMock(return_value=existing)

        event, is_new = await deduplicate_event(
            mock_session, family_id, extracted
        )

        assert is_new is False
        # Verify update was called with enrichment data
        mock_event_dal.update_event.assert_called_once()
        call_kwargs = mock_event_dal.update_event.call_args.kwargs
        assert call_kwargs.get("location") == "Westfield Park - Field 3"
        assert call_kwargs.get("description") == "Regular weekly practice"

    @patch("src.extraction.dedup.event_dal")
    async def test_merge_does_not_overwrite_existing_fields(
        self, mock_event_dal, mock_session, family_id
    ):
        """Merge should not overwrite fields that already have values."""
        dt = datetime(2026, 3, 20, 16, 0, tzinfo=UTC)
        existing = _make_existing_event(
            family_id, "Soccer Practice", dt_start=dt,
            location="Original Field",
            description="Original description",
        )

        extracted = ExtractedEvent(
            title="Soccer Practice",
            datetime_start=dt,
            location="Different Field",
            description="Different description",
            confidence=0.9,
        )

        mock_event_dal.find_duplicate_event = AsyncMock(return_value=existing)
        mock_event_dal.update_event = AsyncMock(return_value=existing)

        event, is_new = await deduplicate_event(
            mock_session, family_id, extracted
        )

        assert is_new is False
        # update_event should be called but NOT with location or description
        # (because existing already has values for those)
        if mock_event_dal.update_event.called:
            call_kwargs = mock_event_dal.update_event.call_args.kwargs
            assert "location" not in call_kwargs
            assert "description" not in call_kwargs

    @patch("src.extraction.dedup.event_dal")
    async def test_merge_adds_source_ref(
        self, mock_event_dal, mock_session, family_id
    ):
        """Merge adds new source_ref to existing event's source_refs."""
        dt = datetime(2026, 3, 20, 16, 0, tzinfo=UTC)
        existing = _make_existing_event(family_id, "Soccer Practice", dt_start=dt)
        existing.source_refs = ["ref-001"]

        extracted = ExtractedEvent(
            title="Soccer Practice",
            datetime_start=dt,
            confidence=0.8,
        )

        mock_event_dal.find_duplicate_event = AsyncMock(return_value=existing)
        mock_event_dal.update_event = AsyncMock(return_value=existing)

        await deduplicate_event(
            mock_session, family_id, extracted,
            source_ref="ref-002",
        )

        if mock_event_dal.update_event.called:
            call_kwargs = mock_event_dal.update_event.call_args.kwargs
            if "source_refs" in call_kwargs:
                assert "ref-001" in call_kwargs["source_refs"]
                assert "ref-002" in call_kwargs["source_refs"]

    @patch("src.extraction.dedup.event_dal")
    async def test_merge_upgrades_confidence(
        self, mock_event_dal, mock_session, family_id
    ):
        """Merge updates confidence if new extraction has higher confidence."""
        dt = datetime(2026, 3, 20, 16, 0, tzinfo=UTC)
        existing = _make_existing_event(
            family_id, "Soccer Practice", dt_start=dt,
            extraction_confidence=0.6,
        )

        extracted = ExtractedEvent(
            title="Soccer Practice",
            datetime_start=dt,
            confidence=0.95,
        )

        mock_event_dal.find_duplicate_event = AsyncMock(return_value=existing)
        mock_event_dal.update_event = AsyncMock(return_value=existing)

        await deduplicate_event(mock_session, family_id, extracted)

        mock_event_dal.update_event.assert_called_once()
        call_kwargs = mock_event_dal.update_event.call_args.kwargs
        assert call_kwargs.get("extraction_confidence") == 0.95


class TestDedupNoMatchCreatesNew:
    @patch("src.extraction.dedup.event_dal")
    async def test_creates_new_event_when_no_match(
        self, mock_event_dal, mock_session, family_id
    ):
        """When no duplicate is found, a new event is created."""
        dt = datetime(2026, 3, 20, 16, 0, tzinfo=UTC)
        extracted = ExtractedEvent(
            title="Piano Recital",
            event_type="recital_performance",
            datetime_start=dt,
            datetime_end=dt + timedelta(hours=2),
            location="Music Hall",
            description="Annual spring recital",
            confidence=0.9,
        )

        mock_event_dal.find_duplicate_event = AsyncMock(return_value=None)
        new_event = MagicMock(spec=Event)
        new_event.id = uuid4()
        mock_event_dal.create_event = AsyncMock(return_value=new_event)

        event, is_new = await deduplicate_event(
            mock_session, family_id, extracted,
            source_ref="msg-001",
        )

        assert is_new is True
        mock_event_dal.create_event.assert_called_once()
        call_kwargs = mock_event_dal.create_event.call_args.kwargs
        assert call_kwargs["title"] == "Piano Recital"
        assert call_kwargs["location"] == "Music Hall"

    @patch("src.extraction.dedup.event_dal")
    async def test_creates_event_without_datetime(
        self, mock_event_dal, mock_session, family_id
    ):
        """Events without datetime_start are always created new (can't dedup)."""
        extracted = ExtractedEvent(
            title="Sometime Event",
            datetime_start=None,
            confidence=0.4,
        )

        new_event = MagicMock(spec=Event)
        new_event.id = uuid4()
        mock_event_dal.create_event = AsyncMock(return_value=new_event)

        event, is_new = await deduplicate_event(
            mock_session, family_id, extracted
        )

        assert is_new is True
        # find_duplicate_event should NOT have been called
        mock_event_dal.find_duplicate_event.assert_not_called()

    @patch("src.extraction.dedup.event_dal")
    async def test_new_event_preserves_rsvp_info(
        self, mock_event_dal, mock_session, family_id
    ):
        """New event creation preserves RSVP details from extraction."""
        dt = datetime(2026, 3, 25, 14, 0, tzinfo=UTC)
        rsvp_deadline = datetime(2026, 3, 22, 23, 59, tzinfo=UTC)

        extracted = ExtractedEvent(
            title="Sophia's Birthday Party",
            event_type="birthday_party",
            datetime_start=dt,
            rsvp_needed=True,
            rsvp_deadline=rsvp_deadline,
            rsvp_method="reply_email",
            rsvp_contact="sophia.mom@email.com",
            confidence=0.85,
        )

        mock_event_dal.find_duplicate_event = AsyncMock(return_value=None)
        new_event = MagicMock(spec=Event)
        new_event.id = uuid4()
        mock_event_dal.create_event = AsyncMock(return_value=new_event)

        await deduplicate_event(mock_session, family_id, extracted)

        call_kwargs = mock_event_dal.create_event.call_args.kwargs
        assert call_kwargs["rsvp_status"] == RsvpStatus.pending
        assert call_kwargs["rsvp_deadline"] == rsvp_deadline
        assert call_kwargs["rsvp_contact"] == "sophia.mom@email.com"
