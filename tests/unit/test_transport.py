"""Tests for transport coordination (gating, conflict detection, routine inference).

Tests the new transport coordination functions in src/agents/calendar.py
with mocked DB and LLM calls.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from src.agents.calendar import (
    auto_assign_single_caregiver,
    check_transport_gating,
    detect_sibling_transport_conflicts,
    format_transport_status,
    populate_transport_defaults,
    track_transport_claim,
)
from src.agents.schemas import Conflict
from src.state.models import (
    Caregiver,
    Child,
    Event,
    EventChild,
    EventSource,
    FamilyLearning,
    RecurringSchedule,
)


# ── Helpers ────────────────────────────────────────────────────────────


def _make_event(
    family_id,
    title="Soccer Practice",
    hours_from_now=24,
    location="Westfield Fields",
    children=None,
    drop_off_by=None,
    pick_up_by=None,
    recurring_schedule_id=None,
):
    now = datetime.now(UTC)
    ev = MagicMock(spec=Event)
    ev.id = uuid4()
    ev.family_id = family_id
    ev.title = title
    ev.datetime_start = now + timedelta(hours=hours_from_now)
    ev.datetime_end = now + timedelta(hours=hours_from_now + 1)
    ev.location = location
    ev.source = EventSource.manual
    ev.type = "sports_practice"
    ev.children = children or []
    ev.drop_off_by = drop_off_by
    ev.pick_up_by = pick_up_by
    ev.recurring_schedule_id = recurring_schedule_id
    return ev


def _make_child(family_id, name="Emma"):
    child = MagicMock(spec=Child)
    child.id = uuid4()
    child.family_id = family_id
    child.name = name
    return child


def _make_caregiver(family_id, name="Sarah", phone="+15551234567"):
    cg = MagicMock(spec=Caregiver)
    cg.id = uuid4()
    cg.family_id = family_id
    cg.name = name
    cg.whatsapp_phone = phone
    cg.is_active = True
    cg.google_refresh_token_encrypted = None
    return cg


def _make_event_child(event_id, child_id, family_id):
    ec = MagicMock(spec=EventChild)
    ec.event_id = event_id
    ec.child_id = child_id
    ec.family_id = family_id
    return ec


# ── Gating tests ──────────────────────────────────────────────────────


class TestCheckTransportGating:
    @pytest.mark.asyncio
    async def test_no_children_skips(self):
        """Families with no children skip transport entirely."""
        session = AsyncMock()
        family_id = uuid4()
        event = _make_event(family_id)

        with (
            patch("src.agents.calendar.children_dal.get_children_for_family",
                  new_callable=AsyncMock, return_value=[]),
        ):
            reason, caregivers = await check_transport_gating(session, family_id, event)

        assert reason == "no_children"
        assert caregivers == []

    @pytest.mark.asyncio
    async def test_no_child_on_event_skips(self):
        """Events without linked children (parent events) skip transport."""
        session = AsyncMock()
        family_id = uuid4()
        child = _make_child(family_id)
        event = _make_event(family_id, children=[])  # no children linked

        with (
            patch("src.agents.calendar.children_dal.get_children_for_family",
                  new_callable=AsyncMock, return_value=[child]),
        ):
            reason, caregivers = await check_transport_gating(session, family_id, event)

        assert reason == "no_child_on_event"
        assert caregivers == []

    @pytest.mark.asyncio
    async def test_single_caregiver_detected(self):
        """Single-caregiver families get auto-assign."""
        session = AsyncMock()
        family_id = uuid4()
        child = _make_child(family_id)
        caregiver = _make_caregiver(family_id)
        ec = _make_event_child(uuid4(), child.id, family_id)
        event = _make_event(family_id, children=[ec])

        with (
            patch("src.agents.calendar.children_dal.get_children_for_family",
                  new_callable=AsyncMock, return_value=[child]),
            patch("src.agents.calendar.families_dal.get_caregivers_for_family",
                  new_callable=AsyncMock, return_value=[caregiver]),
        ):
            reason, caregivers = await check_transport_gating(session, family_id, event)

        assert reason == "single_caregiver"
        assert len(caregivers) == 1

    @pytest.mark.asyncio
    async def test_two_caregivers_passes(self):
        """2+ caregivers with child event passes gating."""
        session = AsyncMock()
        family_id = uuid4()
        child = _make_child(family_id)
        cg1 = _make_caregiver(family_id, "Sarah")
        cg2 = _make_caregiver(family_id, "Mike")
        ec = _make_event_child(uuid4(), child.id, family_id)
        event = _make_event(family_id, children=[ec])

        with (
            patch("src.agents.calendar.children_dal.get_children_for_family",
                  new_callable=AsyncMock, return_value=[child]),
            patch("src.agents.calendar.families_dal.get_caregivers_for_family",
                  new_callable=AsyncMock, return_value=[cg1, cg2]),
        ):
            reason, caregivers = await check_transport_gating(session, family_id, event)

        assert reason is None  # gating passes
        assert len(caregivers) == 2


# ── Sibling conflict detection tests ──────────────────────────────────


class TestDetectSiblingTransportConflicts:
    @pytest.mark.asyncio
    async def test_detects_same_caregiver_same_role_different_child(self):
        """Flags conflict when same caregiver drops off two kids at different locations."""
        family_id = uuid4()
        session = AsyncMock()
        caregiver_id = uuid4()

        emma_id = uuid4()
        jake_id = uuid4()

        # Emma's event at Fieldhouse
        ec_emma = _make_event_child(uuid4(), emma_id, family_id)
        event_emma = _make_event(
            family_id, "Soccer", hours_from_now=24,
            location="Fieldhouse", children=[ec_emma],
            drop_off_by=caregiver_id,
        )

        # Jake's event at Music Center (within ±30 min)
        ec_jake = _make_event_child(uuid4(), jake_id, family_id)
        event_jake = _make_event(
            family_id, "Piano", hours_from_now=24.25,
            location="Music Center", children=[ec_jake],
            drop_off_by=caregiver_id,
        )

        with patch(
            "src.agents.calendar.events_dal.get_events_in_range",
            new_callable=AsyncMock, return_value=[event_emma, event_jake],
        ):
            conflicts = await detect_sibling_transport_conflicts(
                session, family_id, event_emma, "drop_off", caregiver_id
            )

        assert len(conflicts) == 1
        assert conflicts[0].conflict_type == "sibling_transport_conflict"

    @pytest.mark.asyncio
    async def test_no_conflict_different_role(self):
        """No conflict when same caregiver has drop-off for one and pick-up for other."""
        family_id = uuid4()
        session = AsyncMock()
        caregiver_id = uuid4()

        emma_id = uuid4()
        jake_id = uuid4()

        ec_emma = _make_event_child(uuid4(), emma_id, family_id)
        event_emma = _make_event(
            family_id, "Soccer", hours_from_now=24,
            location="Fieldhouse", children=[ec_emma],
            drop_off_by=caregiver_id,
        )

        ec_jake = _make_event_child(uuid4(), jake_id, family_id)
        event_jake = _make_event(
            family_id, "Piano", hours_from_now=24.25,
            location="Music Center", children=[ec_jake],
            pick_up_by=caregiver_id,  # different role
            drop_off_by=None,
        )

        with patch(
            "src.agents.calendar.events_dal.get_events_in_range",
            new_callable=AsyncMock, return_value=[event_emma, event_jake],
        ):
            # Checking drop_off — Jake only has pick_up assigned
            conflicts = await detect_sibling_transport_conflicts(
                session, family_id, event_emma, "drop_off", caregiver_id
            )

        assert len(conflicts) == 0

    @pytest.mark.asyncio
    async def test_no_conflict_same_location(self):
        """No conflict when events are at the same location."""
        family_id = uuid4()
        session = AsyncMock()
        caregiver_id = uuid4()

        emma_id = uuid4()
        jake_id = uuid4()

        ec_emma = _make_event_child(uuid4(), emma_id, family_id)
        event_emma = _make_event(
            family_id, "Soccer", hours_from_now=24,
            location="Fieldhouse", children=[ec_emma],
            drop_off_by=caregiver_id,
        )

        ec_jake = _make_event_child(uuid4(), jake_id, family_id)
        event_jake = _make_event(
            family_id, "Basketball", hours_from_now=24.25,
            location="Fieldhouse", children=[ec_jake],  # same location
            drop_off_by=caregiver_id,
        )

        with patch(
            "src.agents.calendar.events_dal.get_events_in_range",
            new_callable=AsyncMock, return_value=[event_emma, event_jake],
        ):
            conflicts = await detect_sibling_transport_conflicts(
                session, family_id, event_emma, "drop_off", caregiver_id
            )

        assert len(conflicts) == 0

    @pytest.mark.asyncio
    async def test_no_conflict_same_child(self):
        """No conflict when both events are for the same child (not sibling)."""
        family_id = uuid4()
        session = AsyncMock()
        caregiver_id = uuid4()
        emma_id = uuid4()

        ec1 = _make_event_child(uuid4(), emma_id, family_id)
        event1 = _make_event(
            family_id, "Soccer", hours_from_now=24,
            location="Fieldhouse", children=[ec1],
            drop_off_by=caregiver_id,
        )

        ec2 = _make_event_child(uuid4(), emma_id, family_id)
        event2 = _make_event(
            family_id, "Piano", hours_from_now=24.25,
            location="Music Center", children=[ec2],
            drop_off_by=caregiver_id,
        )

        with patch(
            "src.agents.calendar.events_dal.get_events_in_range",
            new_callable=AsyncMock, return_value=[event1, event2],
        ):
            conflicts = await detect_sibling_transport_conflicts(
                session, family_id, event1, "drop_off", caregiver_id
            )

        # Same child — this is child_double_book, not sibling conflict
        assert len(conflicts) == 0


# ── Transport status formatting tests ──────────────────────────────────


class TestFormatTransportStatus:
    def test_fully_assigned_returns_none(self):
        """No status shown when both roles are assigned."""
        family_id = uuid4()
        cg_id = uuid4()
        ec = _make_event_child(uuid4(), uuid4(), family_id)
        event = _make_event(
            family_id, children=[ec], drop_off_by=cg_id, pick_up_by=cg_id
        )
        result = format_transport_status(event, {cg_id: "Sarah"})
        assert result is None

    def test_no_children_returns_none(self):
        """Parent events (no children) return None."""
        family_id = uuid4()
        event = _make_event(family_id, children=[])
        result = format_transport_status(event, {})
        assert result is None

    def test_partially_assigned(self):
        """Shows per-role status when partially assigned."""
        family_id = uuid4()
        cg_id = uuid4()
        ec = _make_event_child(uuid4(), uuid4(), family_id)
        event = _make_event(
            family_id, children=[ec], drop_off_by=cg_id, pick_up_by=None
        )
        result = format_transport_status(event, {cg_id: "Sarah"})
        assert result is not None
        assert "Sarah" in result
        assert "unassigned" in result

    def test_fully_unassigned(self):
        """Shows both roles as unassigned."""
        family_id = uuid4()
        ec = _make_event_child(uuid4(), uuid4(), family_id)
        event = _make_event(
            family_id, children=[ec], drop_off_by=None, pick_up_by=None
        )
        result = format_transport_status(event, {})
        assert result is not None
        assert result.count("unassigned") == 2


# ── Routine inference tests ────────────────────────────────────────────


class TestTrackTransportClaim:
    @pytest.mark.asyncio
    async def test_skips_non_recurring_event(self):
        """No tracking for events not linked to a recurring schedule."""
        session = AsyncMock()
        family_id = uuid4()
        caregiver_id = uuid4()
        event = _make_event(family_id, recurring_schedule_id=None)

        with patch("src.agents.calendar.learning_dal") as mock_dal:
            await track_transport_claim(
                session, family_id, caregiver_id, event, "drop_off"
            )
            mock_dal.get_learning_by_source.assert_not_called()

    @pytest.mark.asyncio
    async def test_creates_counter_on_first_claim(self):
        """First claim creates a counter learning entry."""
        session = AsyncMock()
        family_id = uuid4()
        caregiver_id = uuid4()
        schedule_id = uuid4()
        event = _make_event(family_id, recurring_schedule_id=schedule_id)

        with (
            patch("src.agents.calendar.learning_dal.get_learning_by_source",
                  new_callable=AsyncMock, return_value=None),
            patch("src.agents.calendar.learning_dal.create_learning",
                  new_callable=AsyncMock) as mock_create,
        ):
            await track_transport_claim(
                session, family_id, caregiver_id, event, "drop_off"
            )

        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args
        assert call_kwargs.kwargs["category"] == "transport_claim_counter"
        assert "count:1" in call_kwargs.kwargs["fact"]

    @pytest.mark.asyncio
    async def test_creates_routine_at_threshold(self):
        """After 3 claims, creates a transport_routine learning."""
        session = AsyncMock()
        family_id = uuid4()
        caregiver_id = uuid4()
        schedule_id = uuid4()
        event = _make_event(family_id, recurring_schedule_id=schedule_id)

        # Existing counter at count:2
        counter = MagicMock(spec=FamilyLearning)
        counter.fact = "count:2"
        counter.source = f"caregiver:{caregiver_id}|day:Tuesday|role:drop_off"

        mock_schedule = MagicMock(spec=RecurringSchedule)
        mock_schedule.activity_name = "Soccer"

        caregiver = _make_caregiver(family_id, "Sarah")
        caregiver.id = caregiver_id

        with (
            patch("src.agents.calendar.learning_dal.get_learning_by_source",
                  new_callable=AsyncMock, return_value=counter),
            patch("src.agents.calendar.learning_dal.create_learning",
                  new_callable=AsyncMock) as mock_create,
            patch("src.agents.calendar.families_dal.get_caregivers_for_family",
                  new_callable=AsyncMock, return_value=[caregiver]),
            patch("src.agents.calendar.schedules_dal.get_recurring_schedule",
                  new_callable=AsyncMock, return_value=mock_schedule),
        ):
            await track_transport_claim(
                session, family_id, caregiver_id, event, "drop_off"
            )

        # Should create the transport_routine learning
        assert mock_create.called
        routine_call = mock_create.call_args
        assert routine_call.kwargs["category"] == "transport_routine"
        assert "Sarah" in routine_call.kwargs["fact"]
        assert "drop-off" in routine_call.kwargs["fact"]


# ── Auto-population tests ──────────────────────────────────────────────


class TestPopulateTransportDefaults:
    @pytest.mark.asyncio
    async def test_skips_parent_events(self):
        """Events without children skip transport population."""
        session = AsyncMock()
        family_id = uuid4()
        event = _make_event(family_id, children=[])

        with (
            patch("src.agents.calendar.children_dal.get_children_for_family",
                  new_callable=AsyncMock, return_value=[_make_child(family_id)]),
        ):
            result = await populate_transport_defaults(session, family_id, event)

        assert result["action"] == "skipped"

    @pytest.mark.asyncio
    async def test_auto_assigns_single_caregiver(self):
        """Single caregiver gets both roles assigned silently."""
        session = AsyncMock()
        family_id = uuid4()
        child = _make_child(family_id)
        caregiver = _make_caregiver(family_id)
        ec = _make_event_child(uuid4(), child.id, family_id)
        event = _make_event(family_id, children=[ec])

        with (
            patch("src.agents.calendar.children_dal.get_children_for_family",
                  new_callable=AsyncMock, return_value=[child]),
            patch("src.agents.calendar.families_dal.get_caregivers_for_family",
                  new_callable=AsyncMock, return_value=[caregiver]),
            patch("src.agents.calendar.events_dal.update_event",
                  new_callable=AsyncMock) as mock_update,
        ):
            result = await populate_transport_defaults(session, family_id, event)

        assert result["action"] == "auto_assigned_single"
        mock_update.assert_called_once_with(
            session, family_id, event.id,
            drop_off_by=caregiver.id,
            pick_up_by=caregiver.id,
        )

    @pytest.mark.asyncio
    async def test_populates_from_recurring_schedule_defaults(self):
        """Events linked to a recurring schedule with defaults get auto-populated."""
        session = AsyncMock()
        family_id = uuid4()
        child = _make_child(family_id)
        cg1 = _make_caregiver(family_id, "Sarah")
        cg2 = _make_caregiver(family_id, "Mike")
        schedule_id = uuid4()

        ec = _make_event_child(uuid4(), child.id, family_id)
        event = _make_event(
            family_id, children=[ec],
            recurring_schedule_id=schedule_id,
        )

        schedule = MagicMock(spec=RecurringSchedule)
        schedule.default_drop_off_caregiver = cg1.id
        schedule.default_pick_up_caregiver = cg2.id

        with (
            patch("src.agents.calendar.children_dal.get_children_for_family",
                  new_callable=AsyncMock, return_value=[child]),
            patch("src.agents.calendar.families_dal.get_caregivers_for_family",
                  new_callable=AsyncMock, return_value=[cg1, cg2]),
            patch("src.agents.calendar.schedules_dal.get_recurring_schedule",
                  new_callable=AsyncMock, return_value=schedule),
            patch("src.agents.calendar.events_dal.update_event",
                  new_callable=AsyncMock) as mock_update,
            patch("src.agents.calendar.events_dal.get_events_in_range",
                  new_callable=AsyncMock, return_value=[]),
        ):
            result = await populate_transport_defaults(session, family_id, event)

        assert result["action"] == "auto_populated"
        mock_update.assert_called_once()
        call_kwargs = mock_update.call_args.kwargs
        assert call_kwargs["drop_off_by"] == cg1.id
        assert call_kwargs["pick_up_by"] == cg2.id
