"""Tests for the Intent Router: classification and dispatching."""

import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from src.extraction.router import (
    _check_approval_response,
    _parse_classification_response,
    classify_intent,
    route_intent,
)
from src.extraction.schemas import IntentResult, IntentType

# ── Unit tests for _parse_classification_response ──────────────────────


class TestParseClassificationResponse:
    def test_parse_valid_json(self):
        raw = json.dumps({
            "intent": "add_event",
            "confidence": 0.95,
            "extracted_params": {"title": "Soccer practice"},
        })
        result = _parse_classification_response(raw)
        assert result.intent == IntentType.add_event
        assert result.confidence == 0.95
        assert result.extracted_params["title"] == "Soccer practice"

    def test_parse_json_with_markdown_fences(self):
        raw = '```json\n{"intent": "query_schedule", "confidence": 0.8, "extracted_params": {}}\n```'
        result = _parse_classification_response(raw)
        assert result.intent == IntentType.query_schedule
        assert result.confidence == 0.8

    def test_parse_unknown_intent(self):
        raw = json.dumps({"intent": "nonexistent_type", "confidence": 0.3})
        result = _parse_classification_response(raw)
        assert result.intent == IntentType.unknown

    def test_parse_invalid_json(self):
        raw = "this is not json at all"
        result = _parse_classification_response(raw)
        assert result.intent == IntentType.unknown
        assert result.confidence == 0.0

    def test_parse_json_with_trailing_reasoning(self):
        raw = (
            '{\n  "intent": "general_question",\n  "confidence": 0.95,\n'
            '  "extracted_params": {}\n}\n```\n'
            '**Reasoning:** The user is asking a general question about what info Radar has.'
        )
        result = _parse_classification_response(raw)
        assert result.intent == IntentType.general_question
        assert result.confidence == 0.95

    def test_parse_json_with_nested_braces_and_trailing_text(self):
        """Reproduces the real failure: nested extracted_params + reasoning with braces."""
        raw = (
            '{\n  "intent": "approval_response",\n  "confidence": 0.45,\n'
            '  "extracted_params": {\n    "action": "dismiss",\n'
            '    "reason": "User appears to be asking a general question"\n  }\n}\n'
            '**Reasoning:** The user message doesn\'t address the pending {action}.'
        )
        result = _parse_classification_response(raw)
        assert result.intent == IntentType.approval_response
        assert result.confidence == 0.45
        assert result.extracted_params["action"] == "dismiss"

    def test_parse_missing_fields_uses_defaults(self):
        raw = json.dumps({"intent": "greeting"})
        result = _parse_classification_response(raw)
        assert result.intent == IntentType.greeting
        assert result.confidence == 0.5
        assert result.extracted_params == {}

    @pytest.mark.parametrize(
        "intent_str,expected",
        [
            ("add_event", IntentType.add_event),
            ("query_schedule", IntentType.query_schedule),
            ("modify_event", IntentType.modify_event),
            ("cancel_event", IntentType.cancel_event),
            ("assign_transport", IntentType.assign_transport),
            ("rsvp_response", IntentType.rsvp_response),
            ("share_info", IntentType.share_info),
            ("approval_response", IntentType.approval_response),
            ("general_question", IntentType.general_question),
            ("greeting", IntentType.greeting),
            ("unknown", IntentType.unknown),
        ],
    )
    def test_parse_all_intent_types(self, intent_str, expected):
        raw = json.dumps({"intent": intent_str, "confidence": 0.9})
        result = _parse_classification_response(raw)
        assert result.intent == expected


# ── Unit tests for _check_approval_response ────────────────────────────


class TestCheckApprovalResponse:
    def _make_pending(self, pending_id=None):
        mock = MagicMock()
        mock.id = pending_id or uuid4()
        mock.draft_content = "Draft RSVP email to sophia.mom@email.com"
        return mock

    def test_approve_keywords(self):
        pending = [self._make_pending()]
        for keyword in ["yes", "approve", "send it", "looks good", "go ahead", "ok", "send"]:
            result = _check_approval_response(keyword, pending)
            assert result is not None, f"Expected approval for '{keyword}'"
            assert result.intent == IntentType.approval_response
            assert result.extracted_params["action"] == "approve"
            assert result.pending_action_id == pending[0].id

    def test_dismiss_keywords(self):
        pending = [self._make_pending()]
        for keyword in ["no", "cancel", "dismiss", "nevermind", "skip"]:
            result = _check_approval_response(keyword, pending)
            assert result is not None, f"Expected dismissal for '{keyword}'"
            assert result.intent == IntentType.approval_response
            assert result.extracted_params["action"] == "dismiss"

    def test_edit_instruction_keywords(self):
        pending = [self._make_pending()]
        for msg in ["change the time to 3pm", "edit the subject", "make it more formal"]:
            result = _check_approval_response(msg, pending)
            assert result is not None, f"Expected edit for '{msg}'"
            assert result.extracted_params["action"] == "edit_instruction"

    def test_unrelated_message_returns_none(self):
        pending = [self._make_pending()]
        result = _check_approval_response("Emma has soccer at 4pm tomorrow", pending)
        assert result is None

    def test_empty_pending_list(self):
        # This function is only called when pending_actions is non-empty,
        # but let's verify it handles edge case
        result = _check_approval_response("yes", [])
        assert result is None

    def test_case_insensitive(self):
        pending = [self._make_pending()]
        result = _check_approval_response("YES", pending)
        assert result is not None
        assert result.extracted_params["action"] == "approve"

    def test_approve_with_trailing_text(self):
        pending = [self._make_pending()]
        result = _check_approval_response("yes please", pending)
        # "yes" + space should still match
        assert result is not None
        assert result.extracted_params["action"] == "approve"


# ── Tests for classify_intent (with mocked LLM) ───────────────────────


class TestClassifyIntent:
    @pytest.mark.asyncio
    async def test_classifies_with_llm(self):
        session = AsyncMock()
        family_id = uuid4()
        sender_id = uuid4()

        llm_response = json.dumps({
            "intent": "add_event",
            "confidence": 0.92,
            "extracted_params": {"title": "Soccer practice", "day": "Saturday"},
        })

        with (
            patch("src.extraction.router.pending_dal.get_active_pending", return_value=[]),
            patch("src.extraction.router.memory_dal.get_recent_messages", return_value=[]),
            patch("src.extraction.router.classify", return_value=llm_response),
        ):
            result = await classify_intent(session, family_id, "Soccer practice Saturday 10am", sender_id)

        assert result.intent == IntentType.add_event
        assert result.confidence == 0.92

    @pytest.mark.asyncio
    async def test_pending_action_takes_priority(self):
        session = AsyncMock()
        family_id = uuid4()
        sender_id = uuid4()

        pending = MagicMock()
        pending.id = uuid4()
        pending.draft_content = "Draft RSVP email"

        with (
            patch("src.extraction.router.pending_dal.get_active_pending", return_value=[pending]),
        ):
            result = await classify_intent(session, family_id, "yes", sender_id)

        assert result.intent == IntentType.approval_response
        assert result.extracted_params["action"] == "approve"
        assert result.pending_action_id == pending.id

    @pytest.mark.asyncio
    async def test_pending_action_does_not_intercept_unrelated(self):
        session = AsyncMock()
        family_id = uuid4()
        sender_id = uuid4()

        pending = MagicMock()
        pending.id = uuid4()
        pending.draft_content = "Draft email"

        llm_response = json.dumps({
            "intent": "add_event",
            "confidence": 0.9,
            "extracted_params": {},
        })

        with (
            patch("src.extraction.router.pending_dal.get_active_pending", return_value=[pending]),
            patch("src.extraction.router.memory_dal.get_recent_messages", return_value=[]),
            patch("src.extraction.router.classify", return_value=llm_response),
        ):
            result = await classify_intent(
                session, family_id, "Emma has piano on Wednesdays", sender_id
            )

        assert result.intent == IntentType.add_event

    @pytest.mark.asyncio
    async def test_llm_failure_returns_unknown(self):
        session = AsyncMock()
        family_id = uuid4()
        sender_id = uuid4()

        with (
            patch("src.extraction.router.pending_dal.get_active_pending", return_value=[]),
            patch("src.extraction.router.memory_dal.get_recent_messages", return_value=[]),
            patch("src.extraction.router.classify", side_effect=Exception("API error")),
        ):
            result = await classify_intent(session, family_id, "hello", sender_id)

        assert result.intent == IntentType.unknown
        assert result.confidence == 0.0


# ── Tests for route_intent ─────────────────────────────────────────────


class TestRouteIntent:
    @pytest.mark.asyncio
    async def test_greeting_handler(self):
        session = AsyncMock()
        family_id = uuid4()
        sender_id = uuid4()

        intent = IntentResult(intent=IntentType.greeting, confidence=0.95)
        response = await route_intent(session, family_id, intent, "hi there", sender_id)

        assert "Radar" in response
        assert isinstance(response, str)

    @pytest.mark.asyncio
    async def test_unknown_handler(self):
        session = AsyncMock()
        family_id = uuid4()
        sender_id = uuid4()

        intent = IntentResult(intent=IntentType.unknown, confidence=0.0)
        response = await route_intent(session, family_id, intent, "asdfghjkl", sender_id)

        assert isinstance(response, str)
        assert len(response) > 0

    @pytest.mark.asyncio
    async def test_query_schedule_no_events(self):
        session = AsyncMock()
        family_id = uuid4()
        sender_id = uuid4()

        intent = IntentResult(
            intent=IntentType.query_schedule,
            confidence=0.9,
            extracted_params={"days": 7},
        )

        with patch("src.extraction.router.events_dal.get_upcoming_events", return_value=[]):
            response = await route_intent(
                session, family_id, intent, "What's on this week?", sender_id
            )

        assert "nothing" in response.lower() or "Nothing" in response

    @pytest.mark.asyncio
    async def test_handler_failure_returns_error_message(self):
        session = AsyncMock()
        family_id = uuid4()
        sender_id = uuid4()

        intent = IntentResult(intent=IntentType.add_event, confidence=0.9)

        with patch(
            "src.extraction.router._handle_add_event",
            side_effect=Exception("boom"),
        ):
            response = await route_intent(
                session, family_id, intent, "Add soccer", sender_id
            )

        assert "sorry" in response.lower() or "wrong" in response.lower()

    @pytest.mark.asyncio
    async def test_share_info_creates_child(self):
        session = AsyncMock()
        family_id = uuid4()
        sender_id = uuid4()

        intent = IntentResult(intent=IntentType.share_info, confidence=0.85)

        mock_child = MagicMock()
        mock_child.id = uuid4()
        mock_child.name = "John"

        from pydantic import BaseModel, Field

        class FakeInfo(BaseModel):
            info_type: str = "new_child"
            child_name: str | None = "John"
            value: str | None = None
            fact: str = "My son is John"

        with (
            patch("src.extraction.router.extract", new_callable=AsyncMock, return_value=FakeInfo()) as mock_extract,
            patch("src.extraction.router.children_dal.fuzzy_match_child", new_callable=AsyncMock, return_value=None),
            patch("src.extraction.router.children_dal.create_child", new_callable=AsyncMock, return_value=mock_child) as mock_create,
            patch("src.extraction.router.learning_dal.create_learning", new_callable=AsyncMock) as mock_learn,
        ):
            response = await route_intent(
                session, family_id, intent, "My son is John", sender_id
            )

        mock_create.assert_called_once_with(session, family_id, "John")
        mock_learn.assert_called_once()
        assert "john" in response.lower()
        assert "added" in response.lower() or "got it" in response.lower()

    @pytest.mark.asyncio
    async def test_share_info_updates_school(self):
        session = AsyncMock()
        family_id = uuid4()
        sender_id = uuid4()

        intent = IntentResult(intent=IntentType.share_info, confidence=0.90)

        mock_child = MagicMock()
        mock_child.id = uuid4()
        mock_child.name = "Emma"
        mock_child.school = None

        from pydantic import BaseModel, Field

        class FakeInfo(BaseModel):
            info_type: str = "child_school"
            child_name: str | None = "Emma"
            value: str | None = "Lincoln Elementary"
            fact: str = "Emma goes to Lincoln Elementary"

        with (
            patch("src.extraction.router.extract", new_callable=AsyncMock, return_value=FakeInfo()),
            patch("src.extraction.router.children_dal.fuzzy_match_child", new_callable=AsyncMock, return_value=mock_child),
            patch("src.extraction.router.learning_dal.create_learning", new_callable=AsyncMock) as mock_learn,
        ):
            response = await route_intent(
                session, family_id, intent, "Emma goes to Lincoln Elementary", sender_id
            )

        assert mock_child.school == "Lincoln Elementary"
        mock_learn.assert_called_once()
        assert "lincoln elementary" in response.lower()

    @pytest.mark.asyncio
    async def test_approval_approve(self):
        session = AsyncMock()
        family_id = uuid4()
        sender_id = uuid4()
        action_id = uuid4()

        intent = IntentResult(
            intent=IntentType.approval_response,
            confidence=0.9,
            extracted_params={"action": "approve"},
            pending_action_id=action_id,
        )

        with patch("src.extraction.router.pending_dal.resolve_pending", new_callable=AsyncMock) as mock_resolve, \
             patch("src.extraction.router.pending_dal.get_pending_action", new_callable=AsyncMock, return_value=None):
            response = await route_intent(
                session, family_id, intent, "yes", sender_id
            )

        mock_resolve.assert_called_once()
        assert "approved" in response.lower() or "take care" in response.lower()

    @pytest.mark.asyncio
    async def test_approval_dismiss(self):
        session = AsyncMock()
        family_id = uuid4()
        sender_id = uuid4()
        action_id = uuid4()

        intent = IntentResult(
            intent=IntentType.approval_response,
            confidence=0.9,
            extracted_params={"action": "dismiss"},
            pending_action_id=action_id,
        )

        with patch("src.extraction.router.pending_dal.resolve_pending", new_callable=AsyncMock) as mock_resolve, \
             patch("src.extraction.router.pending_dal.get_pending_action", new_callable=AsyncMock, return_value=None):
            response = await route_intent(
                session, family_id, intent, "no", sender_id
            )

        mock_resolve.assert_called_once()
        assert "dismiss" in response.lower()

    @pytest.mark.asyncio
    async def test_general_question_includes_family_context(self):
        session = AsyncMock()
        family_id = uuid4()
        sender_id = uuid4()

        intent = IntentResult(intent=IntentType.general_question, confidence=0.9)

        fake_context = {
            "family_context": "Children: Emma (age 8), Jake (age 5)\nCaregivers: Mom, Dad",
        }
        with patch(
            "src.agents.context.build_family_context",
            new_callable=AsyncMock,
            return_value=fake_context,
        ) as mock_ctx, patch(
            "src.extraction.router.generate",
            new_callable=AsyncMock,
            return_value="Your family has Emma (8) and Jake (5). Caregivers are Mom and Dad.",
        ) as mock_gen:
            response = await route_intent(
                session, family_id, intent,
                "What do you know about our family?", sender_id,
            )

        mock_ctx.assert_called_once_with(session, family_id, caregiver_id=sender_id)
        # Verify the system prompt includes family context
        call_args = mock_gen.call_args
        system_prompt = call_args[1].get("system") or call_args[0][1]
        assert "Emma" in system_prompt
        assert "Caregivers" in system_prompt
        assert "Emma" in response
