"""Tests for the Reminder Engine (src/agents/reminders.py).

Tests daily digest, weekly summary, and immediate trigger generation.
Mocks LLM calls and database queries.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from src.agents.reminders import (
    check_immediate_triggers,
    generate_daily_digest,
    generate_weekly_summary,
)
from src.state.models import (
    ActionItem,
    ActionItemStatus,
    ActionItemType,
    Event,
    EventSource,

    FamilyLearning,
    PendingAction,
    PendingActionType,
    RsvpStatus,
)


def _mock_session_with_pending_actions(session, pending_actions=None):
    """Configure mock session to return PendingActions from session.execute()."""
    if pending_actions is None:
        pending_actions = []
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = pending_actions
    result_mock = MagicMock()
    result_mock.scalars.return_value = scalars_mock
    session.execute = AsyncMock(return_value=result_mock)
    session.flush = AsyncMock()


@pytest.fixture
def family_id():
    return uuid4()


@pytest.fixture
def mock_session():
    return AsyncMock()


def _make_family(family_id, timezone="America/New_York"):
    family = MagicMock()
    family.id = family_id
    family.timezone = timezone
    return family


def _make_event(
    family_id,
    title="Soccer Practice",
    hours_from_now=2,
    location="Westfield Park",
    drop_off_by=None,
    pick_up_by=None,
    rsvp_status=RsvpStatus.not_applicable,
    rsvp_deadline=None,
):
    now = datetime.now(UTC)
    event = MagicMock(spec=Event)
    event.id = uuid4()
    event.family_id = family_id
    event.title = title
    event.datetime_start = now + timedelta(hours=hours_from_now)
    event.datetime_end = now + timedelta(hours=hours_from_now + 1)
    event.location = location
    event.drop_off_by = drop_off_by
    event.pick_up_by = pick_up_by
    event.rsvp_status = rsvp_status
    event.rsvp_deadline = rsvp_deadline
    event.source = EventSource.manual
    event.type = "sports_practice"
    return event


def _make_action_item(family_id, description="Sign permission slip", hours_until_due=24):
    now = datetime.now(UTC)
    item = MagicMock(spec=ActionItem)
    item.id = uuid4()
    item.family_id = family_id
    item.description = description
    item.due_date = now + timedelta(hours=hours_until_due)
    item.status = ActionItemStatus.pending
    item.type = ActionItemType.form_to_sign
    return item


def _make_learning(family_id, fact="Emma prefers the blue soccer jersey"):
    learning = MagicMock(spec=FamilyLearning)
    learning.id = uuid4()
    learning.family_id = family_id
    learning.fact = fact
    learning.category = "preference"
    learning.surfaced_in_summary = False
    return learning


class TestDailyDigest:
    @patch("src.agents.reminders.generate")
    @patch("src.agents.reminders.children_dal")
    @patch("src.agents.reminders.families_dal")
    @patch("src.agents.reminders.event_dal")
    async def test_generates_digest_with_events(
        self, mock_event_dal, mock_families_dal, mock_children_dal, mock_generate, mock_session, family_id
    ):
        """Daily digest generates content when there are today's events."""
        events = [_make_event(family_id, "Soccer Practice", hours_from_now=4)]

        mock_event_dal.get_events_in_range = AsyncMock(side_effect=[events, events])
        mock_event_dal.get_action_items_due_soon = AsyncMock(return_value=[])
        mock_event_dal.get_unconfirmed_events = AsyncMock(return_value=[])
        mock_families_dal.get_family = AsyncMock(return_value=_make_family(family_id))
        mock_families_dal.get_caregivers_for_family = AsyncMock(return_value=[])
        mock_children_dal.get_children_for_family = AsyncMock(return_value=[])
        mock_generate.return_value = "Good morning! Here's your day:\n- 11:00 AM: Soccer Practice at Westfield Park"
        _mock_session_with_pending_actions(mock_session)

        result = await generate_daily_digest(mock_session, family_id)

        assert result is not None
        assert "Soccer Practice" in result
        mock_generate.assert_called_once()

    @patch("src.agents.reminders.children_dal")
    @patch("src.agents.reminders.families_dal")
    @patch("src.agents.reminders.event_dal")
    async def test_returns_none_when_nothing_actionable(
        self, mock_event_dal, mock_families_dal, mock_children_dal, mock_session, family_id
    ):
        """Daily digest returns None when there are no events, deadlines, or transport needs."""
        mock_families_dal.get_family = AsyncMock(return_value=_make_family(family_id))
        mock_children_dal.get_children_for_family = AsyncMock(return_value=[])
        mock_event_dal.get_events_in_range = AsyncMock(return_value=[])
        mock_event_dal.get_action_items_due_soon = AsyncMock(return_value=[])
        mock_event_dal.get_unconfirmed_events = AsyncMock(return_value=[])
        _mock_session_with_pending_actions(mock_session)

        result = await generate_daily_digest(mock_session, family_id)

        assert result is None

    @patch("src.agents.reminders.generate")
    @patch("src.agents.reminders.children_dal")
    @patch("src.agents.reminders.families_dal")
    @patch("src.agents.reminders.event_dal")
    async def test_includes_deadlines(
        self, mock_event_dal, mock_families_dal, mock_children_dal, mock_generate, mock_session, family_id
    ):
        """Daily digest includes approaching action item deadlines."""
        items = [_make_action_item(family_id, "Sign field trip permission slip")]

        mock_families_dal.get_family = AsyncMock(return_value=_make_family(family_id))
        mock_families_dal.get_caregivers_for_family = AsyncMock(return_value=[])
        mock_children_dal.get_children_for_family = AsyncMock(return_value=[])
        mock_event_dal.get_events_in_range = AsyncMock(return_value=[])
        mock_event_dal.get_action_items_due_soon = AsyncMock(return_value=items)
        mock_event_dal.get_unconfirmed_events = AsyncMock(return_value=[])
        mock_generate.return_value = "Heads up! Due soon:\n- Sign field trip permission slip"
        _mock_session_with_pending_actions(mock_session)

        result = await generate_daily_digest(mock_session, family_id)

        assert result is not None
        # Verify LLM was called with deadline info
        call_args = mock_generate.call_args
        assert "permission slip" in call_args.kwargs.get("prompt", call_args.args[0] if call_args.args else "")

    @patch("src.agents.reminders.generate")
    @patch("src.agents.reminders.children_dal")
    @patch("src.agents.reminders.families_dal")
    @patch("src.agents.reminders.event_dal")
    async def test_includes_unclaimed_transport(
        self, mock_event_dal, mock_families_dal, mock_children_dal, mock_generate, mock_session, family_id
    ):
        """Daily digest flags events with no transport assigned."""
        event = _make_event(
            family_id, "Piano Lesson", hours_from_now=6,
            drop_off_by=None, pick_up_by=None,
        )

        mock_event_dal.get_events_in_range = AsyncMock(side_effect=[[], [event]])
        mock_event_dal.get_action_items_due_soon = AsyncMock(return_value=[])
        mock_event_dal.get_unconfirmed_events = AsyncMock(return_value=[])
        mock_families_dal.get_family = AsyncMock(return_value=_make_family(family_id))
        mock_families_dal.get_caregivers_for_family = AsyncMock(return_value=[])
        mock_children_dal.get_children_for_family = AsyncMock(return_value=[])
        mock_generate.return_value = "Transport needed:\n- Piano Lesson: needs drop-off and pick-up"
        _mock_session_with_pending_actions(mock_session)

        result = await generate_daily_digest(mock_session, family_id)

        assert result is not None


    @patch("src.agents.reminders.generate")
    @patch("src.agents.reminders.children_dal")
    @patch("src.agents.reminders.families_dal")
    @patch("src.agents.reminders.event_dal")
    async def test_includes_pending_confirmations(
        self, mock_event_dal, mock_families_dal, mock_children_dal, mock_generate, mock_session, family_id
    ):
        """Daily digest includes unconfirmed future events in pending confirmations section."""
        unconfirmed_event = _make_event(family_id, "Art Class", hours_from_now=48)
        unconfirmed_event.confirmed_by_caregiver = False

        mock_event_dal.get_events_in_range = AsyncMock(return_value=[])
        mock_event_dal.get_action_items_due_soon = AsyncMock(return_value=[])
        # First call (future_only=False) for auto-cancel, second call (future_only=True) for display
        mock_event_dal.get_unconfirmed_events = AsyncMock(
            side_effect=[[], [unconfirmed_event]]
        )
        mock_families_dal.get_family = AsyncMock(return_value=_make_family(family_id))
        mock_families_dal.get_caregivers_for_family = AsyncMock(return_value=[])
        mock_children_dal.get_children_for_family = AsyncMock(return_value=[])
        mock_generate.return_value = "Pending confirmations:\n- Art Class"
        _mock_session_with_pending_actions(mock_session)

        result = await generate_daily_digest(mock_session, family_id)

        assert result is not None
        # Verify LLM prompt includes pending confirmation text
        call_args = mock_generate.call_args
        prompt = call_args.kwargs.get("prompt", call_args.args[0] if call_args.args else "")
        assert "Pending confirmations" in prompt
        assert "Art Class" in prompt
        assert "confirm [event name]" in prompt

    @patch("src.agents.reminders.generate")
    @patch("src.agents.reminders.children_dal")
    @patch("src.agents.reminders.families_dal")
    @patch("src.agents.reminders.event_dal")
    async def test_pending_confirmation_shows_email_delivery_failure(
        self, mock_event_dal, mock_families_dal, mock_children_dal, mock_generate, mock_session, family_id
    ):
        """Pending confirmation shows email-found message when whatsapp_delivered is False."""
        unconfirmed_event = _make_event(family_id, "Dance Recital", hours_from_now=72)
        unconfirmed_event.confirmed_by_caregiver = False

        # Create a PendingAction with whatsapp_delivered=False
        pa = MagicMock(spec=PendingAction)
        pa.context = {"event_id": str(unconfirmed_event.id), "whatsapp_delivered": False}
        pa.type = PendingActionType.event_confirmation

        mock_event_dal.get_events_in_range = AsyncMock(return_value=[])
        mock_event_dal.get_action_items_due_soon = AsyncMock(return_value=[])
        mock_event_dal.get_unconfirmed_events = AsyncMock(
            side_effect=[[], [unconfirmed_event]]
        )
        mock_families_dal.get_family = AsyncMock(return_value=_make_family(family_id))
        mock_families_dal.get_caregivers_for_family = AsyncMock(return_value=[])
        mock_children_dal.get_children_for_family = AsyncMock(return_value=[])
        mock_generate.return_value = "Pending: Dance Recital"
        _mock_session_with_pending_actions(mock_session, pending_actions=[pa])

        result = await generate_daily_digest(mock_session, family_id)

        assert result is not None
        call_args = mock_generate.call_args
        prompt = call_args.kwargs.get("prompt", call_args.args[0] if call_args.args else "")
        assert "Found in email but couldn't reach you" in prompt

    @patch("src.agents.reminders.children_dal")
    @patch("src.agents.reminders.families_dal")
    @patch("src.agents.reminders.event_dal")
    async def test_auto_cancels_past_unconfirmed_events(
        self, mock_event_dal, mock_families_dal, mock_children_dal, mock_session, family_id
    ):
        """Past unconfirmed events are auto-cancelled at digest time."""
        past_event = _make_event(family_id, "Missed Event", hours_from_now=-24)
        past_event.confirmed_by_caregiver = False
        past_event.cancelled_at = None

        mock_event_dal.get_events_in_range = AsyncMock(return_value=[])
        mock_event_dal.get_action_items_due_soon = AsyncMock(return_value=[])
        mock_event_dal.get_unconfirmed_events = AsyncMock(
            side_effect=[[past_event], []]  # first call returns past event, second returns nothing
        )
        mock_families_dal.get_family = AsyncMock(return_value=_make_family(family_id))
        mock_children_dal.get_children_for_family = AsyncMock(return_value=[])
        _mock_session_with_pending_actions(mock_session)

        result = await generate_daily_digest(mock_session, family_id)

        # Past event should have cancelled_at set
        assert past_event.cancelled_at is not None
        # Nothing else actionable, so digest returns None
        assert result is None


class TestWeeklySummary:
    @patch("src.agents.reminders.generate")
    @patch("src.agents.reminders.children_dal")
    @patch("src.agents.reminders.families_dal")
    @patch("src.agents.reminders.learning_dal")
    @patch("src.agents.reminders.event_dal")
    async def test_always_generates(
        self, mock_event_dal, mock_learning_dal, mock_families_dal, mock_children_dal, mock_generate, mock_session, family_id
    ):
        """Weekly summary always generates, even with no events."""
        mock_families_dal.get_family = AsyncMock(return_value=_make_family(family_id))
        mock_families_dal.get_caregivers_for_family = AsyncMock(return_value=[])
        mock_children_dal.get_children_for_family = AsyncMock(return_value=[])
        mock_event_dal.get_events_in_range = AsyncMock(return_value=[])
        mock_event_dal.get_events_needing_rsvp = AsyncMock(return_value=[])
        mock_learning_dal.get_unsurfaced_learnings = AsyncMock(return_value=[])
        mock_learning_dal.mark_surfaced = AsyncMock()
        mock_learning_dal.auto_confirm_previously_surfaced = AsyncMock(return_value=[])
        mock_generate.return_value = "This week looks clear! No events scheduled."

        result = await generate_weekly_summary(mock_session, family_id)

        assert result is not None
        assert len(result) > 0
        mock_generate.assert_called_once()

    @patch("src.agents.reminders.generate")
    @patch("src.agents.reminders.children_dal")
    @patch("src.agents.reminders.families_dal")
    @patch("src.agents.reminders.learning_dal")
    @patch("src.agents.reminders.event_dal")
    async def test_surfaces_and_marks_learnings(
        self, mock_event_dal, mock_learning_dal, mock_families_dal, mock_children_dal, mock_generate, mock_session, family_id
    ):
        """Weekly summary includes unsurfaced learnings and marks them as surfaced."""
        learnings = [
            _make_learning(family_id, "Emma prefers the blue jersey"),
            _make_learning(family_id, "Jake is allergic to peanuts"),
        ]

        mock_families_dal.get_family = AsyncMock(return_value=_make_family(family_id))
        mock_families_dal.get_caregivers_for_family = AsyncMock(return_value=[])
        mock_children_dal.get_children_for_family = AsyncMock(return_value=[])
        mock_event_dal.get_events_in_range = AsyncMock(return_value=[])
        mock_event_dal.get_events_needing_rsvp = AsyncMock(return_value=[])
        mock_learning_dal.get_unsurfaced_learnings = AsyncMock(return_value=learnings)
        mock_learning_dal.mark_surfaced = AsyncMock()
        mock_learning_dal.auto_confirm_previously_surfaced = AsyncMock(return_value=[])
        mock_generate.return_value = "Weekly summary with learnings"

        await generate_weekly_summary(mock_session, family_id)

        # Verify learnings were marked as surfaced
        mock_learning_dal.mark_surfaced.assert_called_once_with(
            mock_session,
            family_id,
            [learnings[0].id, learnings[1].id],
        )

    @patch("src.agents.reminders.generate")
    @patch("src.agents.reminders.children_dal")
    @patch("src.agents.reminders.families_dal")
    @patch("src.agents.reminders.learning_dal")
    @patch("src.agents.reminders.event_dal")
    async def test_includes_rsvp_events(
        self, mock_event_dal, mock_learning_dal, mock_families_dal, mock_children_dal, mock_generate, mock_session, family_id
    ):
        """Weekly summary includes events needing RSVP."""
        rsvp_event = _make_event(
            family_id,
            "Sophia's Birthday Party",
            hours_from_now=72,
            rsvp_status=RsvpStatus.pending,
            rsvp_deadline=datetime.now(UTC) + timedelta(days=2),
        )

        mock_families_dal.get_family = AsyncMock(return_value=_make_family(family_id))
        mock_families_dal.get_caregivers_for_family = AsyncMock(return_value=[])
        mock_children_dal.get_children_for_family = AsyncMock(return_value=[])
        mock_event_dal.get_events_in_range = AsyncMock(return_value=[rsvp_event])
        mock_event_dal.get_events_needing_rsvp = AsyncMock(return_value=[rsvp_event])
        mock_learning_dal.get_unsurfaced_learnings = AsyncMock(return_value=[])
        mock_learning_dal.mark_surfaced = AsyncMock()
        mock_learning_dal.auto_confirm_previously_surfaced = AsyncMock(return_value=[])
        mock_generate.return_value = "RSVPs needed: Sophia's Birthday Party"

        result = await generate_weekly_summary(mock_session, family_id)

        assert result is not None


class TestImmediateTriggers:
    @patch("src.agents.reminders.families_dal")
    @patch("src.agents.reminders.event_dal")
    async def test_rsvp_deadline_trigger(self, mock_event_dal, mock_families_dal, mock_session, family_id):
        """Triggers notification when RSVP deadline is within 48h."""
        mock_families_dal.get_family = AsyncMock(return_value=_make_family(family_id))

        rsvp_event = _make_event(
            family_id,
            "Birthday Party",
            hours_from_now=72,
            rsvp_status=RsvpStatus.pending,
            rsvp_deadline=datetime.now(UTC) + timedelta(hours=24),
        )

        mock_event_dal.get_events_needing_rsvp = AsyncMock(return_value=[rsvp_event])
        mock_event_dal.get_events_in_range = AsyncMock(return_value=[])

        messages = await check_immediate_triggers(mock_session, family_id)

        assert len(messages) == 1
        assert "Birthday Party" in messages[0]
        assert "RSVP" in messages[0]

    @patch("src.agents.reminders.families_dal")
    @patch("src.agents.reminders.event_dal")
    async def test_unclaimed_transport_trigger(self, mock_event_dal, mock_families_dal, mock_session, family_id):
        """Triggers notification for unclaimed transport within 48h."""
        mock_families_dal.get_family = AsyncMock(return_value=_make_family(family_id))

        event = _make_event(
            family_id, "Soccer Game", hours_from_now=20,
            drop_off_by=None, pick_up_by=uuid4(),  # drop-off unclaimed
        )

        mock_event_dal.get_events_needing_rsvp = AsyncMock(return_value=[])
        mock_event_dal.get_events_in_range = AsyncMock(return_value=[event])

        messages = await check_immediate_triggers(mock_session, family_id)

        assert len(messages) == 1
        assert "drop-off" in messages[0]
        assert "Soccer Game" in messages[0]

    @patch("src.agents.reminders.families_dal")
    @patch("src.agents.reminders.event_dal")
    async def test_no_triggers_when_nothing_urgent(self, mock_event_dal, mock_families_dal, mock_session, family_id):
        """Returns empty list when nothing is urgent."""
        mock_families_dal.get_family = AsyncMock(return_value=_make_family(family_id))
        mock_event_dal.get_events_needing_rsvp = AsyncMock(return_value=[])
        mock_event_dal.get_events_in_range = AsyncMock(return_value=[])

        messages = await check_immediate_triggers(mock_session, family_id)

        assert messages == []
