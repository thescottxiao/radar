"""Tests for ICS attachment processing (src/ingestion/ics.process_ics_attachment)."""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from src.ingestion.ics import process_ics_attachment
from src.state.models import Event

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "ics"


class TestProcessIcsAttachment:
    @pytest.fixture
    def family_id(self):
        return uuid4()

    @pytest.fixture
    def mock_session(self):
        return AsyncMock()

    @patch("src.ingestion.ics.deduplicate_event")
    async def test_valid_ics_returns_events(self, mock_dedup, family_id, mock_session):
        """Valid ICS content returns parsed events."""
        content = (FIXTURES_DIR / "single_event.ics").read_text()

        mock_event = MagicMock(spec=Event)
        mock_event.id = uuid4()
        mock_event.title = "Emma's Birthday Party"
        mock_event.datetime_start = datetime(2026, 4, 15, 18, 0, tzinfo=UTC)
        mock_event.location = "FunZone Indoor Play Center"
        mock_dedup.return_value = (mock_event, True)

        results = await process_ics_attachment(mock_session, family_id, content)

        assert len(results) == 1
        event, is_new = results[0]
        assert event.title == "Emma's Birthday Party"
        assert is_new is True
        mock_dedup.assert_called_once()

    @patch("src.ingestion.ics.deduplicate_event")
    async def test_multi_event_ics(self, mock_dedup, family_id, mock_session):
        """ICS with multiple events returns all of them."""
        content = (FIXTURES_DIR / "soccer_schedule.ics").read_text()

        mock_event = MagicMock(spec=Event)
        mock_event.id = uuid4()
        mock_event.title = "Soccer"
        mock_event.datetime_start = datetime(2026, 3, 21, 10, 0, tzinfo=UTC)
        mock_event.location = None
        mock_dedup.return_value = (mock_event, True)

        results = await process_ics_attachment(mock_session, family_id, content)

        assert len(results) == 4
        assert mock_dedup.call_count == 4

    async def test_invalid_content_returns_empty(self, family_id, mock_session):
        """Non-ICS content returns empty list."""
        results = await process_ics_attachment(
            mock_session, family_id, "<html>Not an ICS file</html>"
        )
        assert results == []

    async def test_empty_content_returns_empty(self, family_id, mock_session):
        """Empty content returns empty list."""
        results = await process_ics_attachment(mock_session, family_id, "")
        assert results == []

    async def test_oversized_content_rejected(self, family_id, mock_session):
        """Content exceeding 1MB is rejected."""
        huge_content = "BEGIN:VCALENDAR\n" + "x" * 1_100_000
        results = await process_ics_attachment(mock_session, family_id, huge_content)
        assert results == []

    async def test_no_begin_vcalendar_rejected(self, family_id, mock_session):
        """Content not starting with BEGIN:VCALENDAR is rejected."""
        results = await process_ics_attachment(
            mock_session, family_id, "This is just plain text"
        )
        assert results == []

    @patch("src.ingestion.ics.deduplicate_event")
    async def test_dedup_marks_existing_events(self, mock_dedup, family_id, mock_session):
        """Duplicate events are returned with is_new=False."""
        content = (FIXTURES_DIR / "single_event.ics").read_text()

        mock_event = MagicMock(spec=Event)
        mock_event.id = uuid4()
        mock_event.title = "Emma's Birthday Party"
        mock_event.datetime_start = datetime(2026, 4, 15, 18, 0, tzinfo=UTC)
        mock_event.location = "FunZone Indoor Play Center"
        mock_dedup.return_value = (mock_event, False)  # Not new

        results = await process_ics_attachment(mock_session, family_id, content)

        assert len(results) == 1
        _, is_new = results[0]
        assert is_new is False
