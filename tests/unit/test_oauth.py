"""Tests for OAuth URL generation and state JWT encoding/decoding."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

import jwt
import pytest
from src.auth.oauth import _JWT_ALGORITHM, SCOPES, build_oauth_url, decode_state


@pytest.fixture
def mock_settings():
    """Mock settings for OAuth tests."""
    with patch("src.auth.oauth.settings") as mock:
        mock.token_encryption_key.get_secret_value.return_value = "test-signing-key-for-jwt-tokens-1234"
        mock.google_client_id = "test-client-id.apps.googleusercontent.com"
        mock.google_client_secret.get_secret_value.return_value = "test-client-secret"
        mock.google_redirect_uri = "http://localhost:8000/auth/google/callback"
        yield mock


class TestBuildOAuthUrl:
    def test_generates_valid_url(self, mock_settings):
        family_id = uuid4()
        phone = "+15551234567"

        url = build_oauth_url(family_id, phone)

        assert url.startswith("https://accounts.google.com/o/oauth2/auth")
        assert "client_id=test-client-id.apps.googleusercontent.com" in url
        assert "redirect_uri=" in url
        assert "state=" in url
        assert "access_type=offline" in url
        assert "prompt=consent" in url

    def test_includes_correct_scopes(self, mock_settings):
        url = build_oauth_url(uuid4(), "+15551234567")

        # Check that required scopes are in the URL
        assert "gmail.readonly" in url
        assert "calendar.events" in url

    def test_state_contains_family_and_phone(self, mock_settings):
        family_id = uuid4()
        phone = "+15559876543"

        url = build_oauth_url(family_id, phone)

        # Extract state param from URL
        import urllib.parse

        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        state_token = params["state"][0]

        # Decode the state JWT
        decoded = decode_state(state_token)
        assert decoded["family_id"] == family_id
        assert decoded["caregiver_phone"] == phone

    def test_different_families_get_different_urls(self, mock_settings):
        url1 = build_oauth_url(uuid4(), "+15551111111")
        url2 = build_oauth_url(uuid4(), "+15552222222")

        assert url1 != url2


class TestDecodeState:
    def test_roundtrip_encode_decode(self, mock_settings):
        family_id = uuid4()
        phone = "+15551234567"

        url = build_oauth_url(family_id, phone)

        # Extract state from URL
        import urllib.parse

        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        state_token = params["state"][0]

        result = decode_state(state_token)
        assert result["family_id"] == family_id
        assert result["caregiver_phone"] == phone

    def test_expired_state_raises(self, mock_settings):
        signing_key = mock_settings.token_encryption_key.get_secret_value.return_value

        expired_payload = {
            "family_id": str(uuid4()),
            "caregiver_phone": "+15551234567",
            "exp": datetime.now(UTC) - timedelta(hours=1),
        }
        expired_token = jwt.encode(
            expired_payload, signing_key, algorithm=_JWT_ALGORITHM
        )

        with pytest.raises(jwt.ExpiredSignatureError):
            decode_state(expired_token)

    def test_invalid_signature_raises(self, mock_settings):
        payload = {
            "family_id": str(uuid4()),
            "caregiver_phone": "+15551234567",
            "exp": datetime.now(UTC) + timedelta(hours=1),
        }
        bad_token = jwt.encode(payload, "wrong-key", algorithm=_JWT_ALGORITHM)

        with pytest.raises(jwt.InvalidSignatureError):
            decode_state(bad_token)

    def test_tampered_token_raises(self, mock_settings):
        signing_key = mock_settings.token_encryption_key.get_secret_value.return_value

        payload = {
            "family_id": str(uuid4()),
            "caregiver_phone": "+15551234567",
            "exp": datetime.now(UTC) + timedelta(hours=1),
        }
        token = jwt.encode(payload, signing_key, algorithm=_JWT_ALGORITHM)

        # Tamper with the token
        tampered = token[:-5] + "XXXXX"

        with pytest.raises(Exception):  # jwt.DecodeError or InvalidSignatureError
            decode_state(tampered)


class TestScopesConstant:
    def test_scopes_include_gmail_readonly(self):
        assert "https://www.googleapis.com/auth/gmail.readonly" in SCOPES

    def test_scopes_include_calendar_events(self):
        assert "https://www.googleapis.com/auth/calendar.events" in SCOPES

    def test_scopes_do_not_include_gmail_send(self):
        """Radar never sends from caregiver Gmail."""
        for scope in SCOPES:
            assert "gmail.send" not in scope
            assert "gmail.modify" not in scope
            assert "gmail.compose" not in scope
