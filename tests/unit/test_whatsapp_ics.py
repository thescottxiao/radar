"""Tests for WhatsApp ICS document upload handling."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from src.ingestion.whatsapp import _extract_message_from_payload, _is_ics_file

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "ics"


class TestIsIcsFile:
    def test_ics_extension(self):
        assert _is_ics_file("schedule.ics", "") is True

    def test_ics_extension_uppercase(self):
        assert _is_ics_file("CALENDAR.ICS", "") is True

    def test_text_calendar_mime(self):
        assert _is_ics_file("calendar", "text/calendar") is True

    def test_application_ics_mime(self):
        assert _is_ics_file("file", "application/ics") is True

    def test_pdf_rejected(self):
        assert _is_ics_file("document.pdf", "application/pdf") is False

    def test_jpg_rejected(self):
        assert _is_ics_file("photo.jpg", "image/jpeg") is False

    def test_empty_rejected(self):
        assert _is_ics_file("", "") is False


class TestExtractDocumentPayload:
    def _make_payload(self, msg_type="document", filename="schedule.ics",
                      mime_type="text/calendar", media_id="media-123"):
        """Build a Meta Cloud API webhook payload for a document message."""
        msg = {
            "from": "15551234567",
            "id": "msg-001",
            "timestamp": "1234567890",
            "type": msg_type,
        }
        if msg_type == "document":
            msg["document"] = {
                "filename": filename,
                "mime_type": mime_type,
                "id": media_id,
            }

        return {
            "object": "whatsapp_business_account",
            "entry": [{
                "id": "entry-1",
                "changes": [{
                    "value": {
                        "messaging_product": "whatsapp",
                        "metadata": {"display_phone_number": "15559876543", "phone_number_id": "pn-1"},
                        "contacts": [{"profile": {"name": "Test User"}, "wa_id": "15551234567"}],
                        "messages": [msg],
                    },
                    "field": "messages",
                }],
            }],
        }

    def test_ics_document_extracted(self):
        """ICS document messages are extracted with document metadata."""
        payload = self._make_payload()
        result = _extract_message_from_payload(payload)

        assert result is not None
        assert result["sender_phone"] == "+15551234567"
        assert result["text"] == ""
        assert result["document"]["media_id"] == "media-123"
        assert result["document"]["filename"] == "schedule.ics"
        assert result["document"]["mime_type"] == "text/calendar"

    def test_non_ics_document_rejected(self):
        """Non-ICS documents return None."""
        payload = self._make_payload(filename="report.pdf", mime_type="application/pdf")
        result = _extract_message_from_payload(payload)

        assert result is None

    def test_text_message_still_works(self):
        """Text messages continue to work normally."""
        payload = self._make_payload(msg_type="text")
        # Override to be a text message
        msg = payload["entry"][0]["changes"][0]["value"]["messages"][0]
        msg.pop("document", None)
        msg["text"] = {"body": "Hello world"}

        result = _extract_message_from_payload(payload)

        assert result is not None
        assert result["text"] == "Hello world"
        assert "document" not in result


class TestHandleIcsUpload:
    @pytest.fixture
    def family(self):
        family = MagicMock()
        family.id = uuid4()
        return family

    @pytest.fixture
    def caregiver(self):
        cg = MagicMock()
        cg.id = uuid4()
        return cg

    @pytest.fixture
    def mock_session(self):
        return AsyncMock()

    @patch("src.ingestion.whatsapp._download_whatsapp_media")
    @patch("src.ingestion.ics.process_ics_attachment")
    @patch("src.state.pending.create_pending_action")
    @patch("src.actions.whatsapp.send_buttons_to_family")
    async def test_successful_upload(
        self, mock_send, mock_pending, mock_process, mock_download,
        family, caregiver, mock_session
    ):
        """Successful ICS upload creates pending action and sends confirmation."""
        from src.ingestion.whatsapp import _handle_ics_upload

        ics_content = (FIXTURES_DIR / "single_event.ics").read_text()
        mock_download.return_value = ics_content

        mock_event = MagicMock()
        mock_event.id = uuid4()
        mock_event.title = "Emma's Birthday Party"
        mock_event.datetime_start = MagicMock()
        mock_event.datetime_start.strftime.return_value = "Apr 15, 06:00 PM"
        mock_event.location = "FunZone"
        mock_process.return_value = [(mock_event, True)]

        mock_pending.return_value = MagicMock(id=uuid4())

        document = {"media_id": "media-123", "filename": "party.ics", "mime_type": "text/calendar"}
        result = await _handle_ics_upload(mock_session, family, caregiver, document)

        assert "1 new event" in result
        mock_download.assert_called_once_with("media-123")
        mock_pending.assert_called_once()
        mock_send.assert_called_once()

    @patch("src.ingestion.whatsapp._download_whatsapp_media")
    async def test_download_failure(self, mock_download, family, caregiver, mock_session):
        """Download failure returns error message."""
        from src.ingestion.whatsapp import _handle_ics_upload

        mock_download.side_effect = Exception("Network error")

        document = {"media_id": "bad-id", "filename": "test.ics", "mime_type": "text/calendar"}
        result = await _handle_ics_upload(mock_session, family, caregiver, document)

        assert "couldn't download" in result.lower()

    @patch("src.ingestion.whatsapp._download_whatsapp_media")
    @patch("src.ingestion.ics.process_ics_attachment")
    async def test_no_events_found(self, mock_process, mock_download, family, caregiver, mock_session):
        """No events in file returns appropriate message."""
        from src.ingestion.whatsapp import _handle_ics_upload

        mock_download.return_value = "BEGIN:VCALENDAR\nVERSION:2.0\nEND:VCALENDAR"
        mock_process.return_value = []

        document = {"media_id": "media-123", "filename": "empty.ics", "mime_type": "text/calendar"}
        result = await _handle_ics_upload(mock_session, family, caregiver, document)

        assert "couldn't find any events" in result.lower()

    @patch("src.ingestion.whatsapp._download_whatsapp_media")
    @patch("src.ingestion.ics.process_ics_attachment")
    async def test_all_duplicates(self, mock_process, mock_download, family, caregiver, mock_session):
        """All duplicate events returns appropriate message."""
        from src.ingestion.whatsapp import _handle_ics_upload

        mock_download.return_value = (FIXTURES_DIR / "single_event.ics").read_text()

        mock_event = MagicMock()
        mock_event.id = uuid4()
        mock_process.return_value = [(mock_event, False)]  # All duplicates

        document = {"media_id": "media-123", "filename": "test.ics", "mime_type": "text/calendar"}
        result = await _handle_ics_upload(mock_session, family, caregiver, document)

        assert "already on your calendar" in result.lower()
