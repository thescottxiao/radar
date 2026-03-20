"""Tests for Gmail attachment extraction and ICS processing."""

import base64
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from src.ingestion.gmail import (
    _extract_attachments,
    _walk_for_attachments,
)
from src.ingestion.ics import is_ics_file


class TestExtractAttachments:
    def test_single_attachment(self):
        """Extract attachment from a simple message with one attachment."""
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": base64.urlsafe_b64encode(b"email body").decode(), "size": 10},
                },
                {
                    "mimeType": "text/calendar",
                    "filename": "invite.ics",
                    "body": {"attachmentId": "att-001", "size": 512},
                },
            ],
        }

        attachments = _extract_attachments(payload)

        assert len(attachments) == 1
        assert attachments[0]["filename"] == "invite.ics"
        assert attachments[0]["mime_type"] == "text/calendar"
        assert attachments[0]["attachment_id"] == "att-001"
        assert attachments[0]["size"] == 512

    def test_nested_multipart(self):
        """Extract attachment from nested multipart message."""
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {"mimeType": "text/plain", "body": {"data": "dGVzdA==", "size": 4}},
                        {"mimeType": "text/html", "body": {"data": "dGVzdA==", "size": 4}},
                    ],
                },
                {
                    "mimeType": "text/calendar",
                    "filename": "schedule.ics",
                    "body": {"attachmentId": "att-002", "size": 1024},
                },
            ],
        }

        attachments = _extract_attachments(payload)

        assert len(attachments) == 1
        assert attachments[0]["filename"] == "schedule.ics"

    def test_no_attachments(self):
        """Message without attachments returns empty list."""
        payload = {
            "mimeType": "text/plain",
            "body": {"data": "dGVzdA==", "size": 4},
        }

        attachments = _extract_attachments(payload)
        assert attachments == []

    def test_multiple_attachments(self):
        """Multiple attachments are all extracted."""
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": "dGVzdA=="}},
                {
                    "mimeType": "text/calendar",
                    "filename": "event1.ics",
                    "body": {"attachmentId": "att-001", "size": 256},
                },
                {
                    "mimeType": "application/pdf",
                    "filename": "flyer.pdf",
                    "body": {"attachmentId": "att-002", "size": 50000},
                },
            ],
        }

        attachments = _extract_attachments(payload)
        assert len(attachments) == 2
        filenames = [a["filename"] for a in attachments]
        assert "event1.ics" in filenames
        assert "flyer.pdf" in filenames

    def test_part_without_attachment_id_skipped(self):
        """Parts without attachmentId are not treated as attachments."""
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "text/calendar",
                    "filename": "inline.ics",
                    "body": {"data": "dGVzdA==", "size": 100},  # Inline, no attachmentId
                },
            ],
        }

        attachments = _extract_attachments(payload)
        assert attachments == []


class TestIsIcsFile:
    def test_ics_extension(self):
        assert is_ics_file("event.ics", "application/octet-stream") is True

    def test_text_calendar_mime(self):
        assert is_ics_file("invite", "text/calendar") is True

    def test_application_ics_mime(self):
        assert is_ics_file("file", "application/ics") is True

    def test_pdf_not_ics(self):
        assert is_ics_file("doc.pdf", "application/pdf") is False

    def test_ics_extension_case_insensitive(self):
        assert is_ics_file("CALENDAR.ICS", "") is True


class TestDownloadGmailAttachment:
    @patch("src.ingestion.gmail.httpx.AsyncClient")
    async def test_download_and_decode(self, mock_client_cls):
        """Downloads and base64url-decodes attachment content."""
        from src.ingestion.gmail import download_gmail_attachment

        ics_bytes = b"BEGIN:VCALENDAR\nVERSION:2.0\nEND:VCALENDAR"
        encoded = base64.urlsafe_b64encode(ics_bytes).decode()

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"data": encoded}
        mock_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_cls.return_value = mock_client

        result = await download_gmail_attachment("token-123", "msg-001", "att-001")

        assert result == "BEGIN:VCALENDAR\nVERSION:2.0\nEND:VCALENDAR"
        mock_client.get.assert_called_once()
