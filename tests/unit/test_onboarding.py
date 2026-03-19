"""Unit tests for the Onboarding Agent.

Tests children extraction from natural language (mock LLM)
and onboarding step detection.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from src.agents.onboarding import (
    _build_oauth_url,
    _determine_step,
    _format_children_summary,
    get_welcome_message,
    handle_onboarding_message,
)
from src.agents.schemas import ExtractedChild, OnboardingExtraction
from src.state.models import Caregiver, Child, Family

# ── Fixtures ────────────────────────────────────────────────────────────


def _make_family(onboarding_complete: bool = False) -> MagicMock:
    mock = MagicMock(spec=Family)
    mock.id = uuid4()
    mock.onboarding_complete = onboarding_complete
    mock.timezone = "America/New_York"
    mock.updated_at = datetime.now(UTC)
    return mock


def _make_caregiver(
    family_id=None,
    has_google: bool = False,
    phone: str = "+15551234567",
    name: str | None = "Sarah",
) -> MagicMock:
    mock = MagicMock(spec=Caregiver)
    mock.id = uuid4()
    mock.family_id = family_id or uuid4()
    mock.whatsapp_phone = phone
    mock.name = name
    mock.google_refresh_token_encrypted = b"encrypted" if has_google else None
    mock.is_active = True
    return mock


def _make_child(name: str = "Emma", activities: list | None = None) -> MagicMock:
    mock = MagicMock(spec=Child)
    mock.id = uuid4()
    mock.name = name
    mock.activities = activities or []
    return mock


# ── Step detection tests ───────────────────────────────────────────────


class TestDetermineStep:
    """Test onboarding step detection based on state."""

    @pytest.mark.asyncio
    async def test_step_1_no_children(self):
        """No children -> step 1."""
        session = AsyncMock()
        family = _make_family()
        family_id = family.id

        with (
            patch("src.agents.onboarding.children_dal.get_children_for_family", new_callable=AsyncMock, return_value=[]),
            patch("src.agents.onboarding.families_dal.get_caregivers_for_family", new_callable=AsyncMock, return_value=[_make_caregiver()]),
        ):
            step = await _determine_step(session, family_id, family)

        assert step == 1

    @pytest.mark.asyncio
    async def test_step_2_children_no_oauth(self):
        """Children present but no OAuth -> step 2."""
        session = AsyncMock()
        family = _make_family()
        family_id = family.id

        with (
            patch("src.agents.onboarding.children_dal.get_children_for_family", new_callable=AsyncMock, return_value=[_make_child()]),
            patch("src.agents.onboarding.families_dal.get_caregivers_for_family", new_callable=AsyncMock, return_value=[_make_caregiver(has_google=False)]),
        ):
            step = await _determine_step(session, family_id, family)

        assert step == 2

    @pytest.mark.asyncio
    async def test_step_3_children_and_oauth(self):
        """Children + OAuth tokens -> step 3."""
        session = AsyncMock()
        family = _make_family()
        family_id = family.id

        with (
            patch("src.agents.onboarding.children_dal.get_children_for_family", new_callable=AsyncMock, return_value=[_make_child()]),
            patch("src.agents.onboarding.families_dal.get_caregivers_for_family", new_callable=AsyncMock, return_value=[_make_caregiver(has_google=True)]),
        ):
            step = await _determine_step(session, family_id, family)

        assert step == 3


# ── Children extraction tests ──────────────────────────────────────────


class TestHandleOnboardingStep1:
    """Test children extraction from natural language via LLM (mocked)."""

    @pytest.mark.asyncio
    async def test_extracts_children(self):
        """Verify children are extracted and created from natural language."""
        family = _make_family()
        session = AsyncMock()
        sender_phone = "+15551234567"

        extraction = OnboardingExtraction(
            children=[
                ExtractedChild(name="Emma", age=8, activities=["soccer", "piano"]),
                ExtractedChild(name="Jake", age=6, activities=["swim"]),
            ],
            caregiver_name="Sarah",
        )

        _make_child("Emma", ["soccer", "piano"])
        _make_child("Jake", ["swim"])
        mock_caregiver = _make_caregiver(family_id=family.id, phone=sender_phone, name=None)

        create_child_calls = []

        async def mock_create_child(session, family_id, name, date_of_birth=None):
            mock = _make_child(name)
            mock.activities = None  # Will be set after creation
            create_child_calls.append(name)
            return mock

        with (
            patch("src.agents.onboarding.families_dal.get_family", new_callable=AsyncMock, return_value=family),
            patch("src.agents.onboarding.children_dal.get_children_for_family", new_callable=AsyncMock, return_value=[]),
            patch("src.agents.onboarding.families_dal.get_caregivers_for_family", new_callable=AsyncMock, return_value=[mock_caregiver]),
            patch("src.agents.onboarding.extract", new_callable=AsyncMock, return_value=extraction),
            patch("src.agents.onboarding.children_dal.create_child", new_callable=AsyncMock, side_effect=mock_create_child),
            patch("src.agents.onboarding.families_dal.get_caregiver_by_phone", new_callable=AsyncMock, return_value=mock_caregiver),
        ):
            result = await handle_onboarding_message(
                session, family.id,
                "I'm Sarah, my kids are Emma (8) who does soccer and piano, and Jake (6) who swims",
                sender_phone,
            )

        assert "Emma" in result
        assert "Jake" in result
        assert len(create_child_calls) == 2
        assert "Emma" in create_child_calls
        assert "Jake" in create_child_calls

    @pytest.mark.asyncio
    async def test_no_children_extracted(self):
        """Message with no children info should prompt for re-entry."""
        family = _make_family()
        session = AsyncMock()

        extraction = OnboardingExtraction(children=[], caregiver_name=None)

        with (
            patch("src.agents.onboarding.families_dal.get_family", new_callable=AsyncMock, return_value=family),
            patch("src.agents.onboarding.children_dal.get_children_for_family", new_callable=AsyncMock, return_value=[]),
            patch("src.agents.onboarding.families_dal.get_caregivers_for_family", new_callable=AsyncMock, return_value=[_make_caregiver()]),
            patch("src.agents.onboarding.extract", new_callable=AsyncMock, return_value=extraction),
        ):
            result = await handle_onboarding_message(
                session, family.id,
                "hello there!",
                "+15551234567",
            )

        assert "didn't catch" in result.lower() or "names" in result.lower()

    @pytest.mark.asyncio
    async def test_already_onboarded(self):
        """Already-onboarded family gets a different message."""
        family = _make_family(onboarding_complete=True)
        session = AsyncMock()

        with patch("src.agents.onboarding.families_dal.get_family", new_callable=AsyncMock, return_value=family):
            result = await handle_onboarding_message(
                session, family.id,
                "hello",
                "+15551234567",
            )

        assert "already set up" in result.lower()


# ── OAuth skip tests ───────────────────────────────────────────────────


class TestHandleOnboardingStep2:

    @pytest.mark.asyncio
    async def test_skip_oauth(self):
        """Saying 'skip' should complete onboarding without OAuth."""
        family = _make_family()
        session = AsyncMock()

        with (
            patch("src.agents.onboarding.families_dal.get_family", new_callable=AsyncMock, return_value=family),
            patch("src.agents.onboarding.children_dal.get_children_for_family", new_callable=AsyncMock, return_value=[_make_child()]),
            patch("src.agents.onboarding.families_dal.get_caregivers_for_family", new_callable=AsyncMock, return_value=[_make_caregiver(has_google=False)]),
        ):
            result = await handle_onboarding_message(
                session, family.id,
                "skip",
                "+15551234567",
            )

        assert "all set" in result.lower() or "set up" in result.lower()
        assert family.onboarding_complete is True


# ── Helper tests ───────────────────────────────────────────────────────


class TestFormatChildrenSummary:

    def test_single_child(self):
        children = [ExtractedChild(name="Emma", age=8, activities=["soccer"])]
        result = _format_children_summary(children)
        assert "Emma" in result
        assert "8" in result
        assert "soccer" in result

    def test_two_children(self):
        children = [
            ExtractedChild(name="Emma", age=8),
            ExtractedChild(name="Jake", age=6),
        ]
        result = _format_children_summary(children)
        assert "Emma" in result
        assert "Jake" in result
        assert " and " in result

    def test_three_children(self):
        children = [
            ExtractedChild(name="Emma", age=8),
            ExtractedChild(name="Jake", age=6),
            ExtractedChild(name="Lily", age=4),
        ]
        result = _format_children_summary(children)
        assert "Emma" in result
        assert "Jake" in result
        assert "Lily" in result
        assert ", and " in result


class TestBuildOauthUrl:

    def test_url_contains_required_params(self):
        family_id = uuid4()
        url = _build_oauth_url(family_id, "+15551234567")
        assert "accounts.google.com" in url
        assert "response_type=code" in url
        assert "access_type=offline" in url
        assert str(family_id) in url


class TestWelcomeMessage:

    @pytest.mark.asyncio
    async def test_welcome_message(self):
        msg = await get_welcome_message()
        assert "Radar" in msg
        assert "kids" in msg.lower() or "children" in msg.lower()
