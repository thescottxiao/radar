"""Tests for event attendee (caregiver) features.

Covers: EventCaregiver model, link/replace DAL functions,
dedup with child_ids, _fuzzy_match_caregiver, caregiver conflict detection,
and attendee fields on extraction/resolution schemas.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from src.agents.calendar import _fuzzy_match_caregiver, detect_conflicts
from src.agents.schemas import (
    Conflict,
    ExtractedEvent,
    ExtractedUpdate,
    ResolvedEvent,
)
from src.extraction.email import ExtractedEvent as EmailExtractedEvent
from src.state.models import EventCaregiver, EventChild


@pytest.fixture
def family_id():
    return uuid4()


@pytest.fixture
def mock_session():
    return AsyncMock()


def _make_caregiver(name, cg_id=None):
    cg = MagicMock()
    cg.id = cg_id or uuid4()
    cg.name = name
    return cg


def _make_existing_event(title, dt_start, child_ids=None, caregiver_ids=None, location=None):
    event = MagicMock()
    event.id = uuid4()
    event.title = title
    event.datetime_start = dt_start
    event.datetime_end = dt_start + timedelta(hours=1)
    event.location = location
    event.cancelled_at = None
    # Children
    children = []
    for cid in (child_ids or []):
        ec = MagicMock(spec=EventChild)
        ec.child_id = cid
        children.append(ec)
    event.children = children
    # Caregivers
    caregivers = []
    for cgid in (caregiver_ids or []):
        ec = MagicMock(spec=EventCaregiver)
        ec.caregiver_id = cgid
        caregivers.append(ec)
    event.caregivers = caregivers
    return event


# ── EventCaregiver model ────────────────────────────────────────────────


class TestEventCaregiverModel:
    def test_has_required_fields(self):
        """EventCaregiver has event_id, caregiver_id, family_id columns."""
        mapper = EventCaregiver.__table__.columns
        assert "event_id" in mapper
        assert "caregiver_id" in mapper
        assert "family_id" in mapper

    def test_table_name(self):
        assert EventCaregiver.__tablename__ == "event_caregivers"


# ── link_caregivers_to_event ────────────────────────────────────────────


class TestLinkCaregiversToEvent:
    @pytest.mark.asyncio
    async def test_adds_links_and_flushes(self, mock_session, family_id):
        from src.state.events import link_caregivers_to_event

        cg1, cg2 = uuid4(), uuid4()
        event_id = uuid4()

        await link_caregivers_to_event(mock_session, family_id, event_id, [cg1, cg2])

        assert mock_session.add.call_count == 2
        mock_session.flush.assert_awaited_once()

        added = [call.args[0] for call in mock_session.add.call_args_list]
        assert all(isinstance(a, EventCaregiver) for a in added)
        assert {a.caregiver_id for a in added} == {cg1, cg2}

    @pytest.mark.asyncio
    async def test_empty_list_no_adds(self, mock_session, family_id):
        from src.state.events import link_caregivers_to_event

        await link_caregivers_to_event(mock_session, family_id, uuid4(), [])

        mock_session.add.assert_not_called()
        mock_session.flush.assert_awaited_once()


# ── replace_children_on_event / replace_caregivers_on_event ─────────────


class TestReplaceLinksOnEvent:
    @pytest.mark.asyncio
    async def test_replace_children_deletes_then_adds(self, mock_session, family_id):
        from src.state.events import replace_children_on_event

        event_id = uuid4()
        await replace_children_on_event(mock_session, family_id, event_id, [uuid4()])

        # Should execute a delete and then add + flush
        mock_session.execute.assert_awaited_once()
        assert mock_session.add.call_count == 1
        added = mock_session.add.call_args.args[0]
        assert isinstance(added, EventChild)

    @pytest.mark.asyncio
    async def test_replace_caregivers_deletes_then_adds(self, mock_session, family_id):
        from src.state.events import replace_caregivers_on_event

        event_id = uuid4()
        cg1, cg2 = uuid4(), uuid4()
        await replace_caregivers_on_event(mock_session, family_id, event_id, [cg1, cg2])

        mock_session.execute.assert_awaited_once()
        assert mock_session.add.call_count == 2
        added = [call.args[0] for call in mock_session.add.call_args_list]
        assert all(isinstance(a, EventCaregiver) for a in added)
        assert {a.caregiver_id for a in added} == {cg1, cg2}


# ── find_duplicate_event with child_ids ─────────────────────────────────


class TestDedupChildFilter:
    @pytest.mark.asyncio
    @patch("src.state.events.select")
    async def test_same_title_time_different_children_no_dup(self, _mock_select, mock_session, family_id):
        """Same title + time but different children should NOT dedup."""
        from src.state.events import find_duplicate_event

        child_a, child_b = uuid4(), uuid4()
        dt = datetime(2026, 4, 1, 16, 0, tzinfo=UTC)
        candidate = _make_existing_event("Soccer Practice", dt, child_ids=[child_a])

        # Mock the query to return our candidate
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [candidate]
        mock_session.execute.return_value = mock_result

        result = await find_duplicate_event(
            mock_session, family_id, "Soccer Practice", dt,
            child_ids=[child_b],
        )
        assert result is None

    @pytest.mark.asyncio
    @patch("src.state.events.select")
    async def test_same_title_time_same_children_is_dup(self, _mock_select, mock_session, family_id):
        """Same title + time + same children should dedup."""
        from src.state.events import find_duplicate_event

        child_a = uuid4()
        dt = datetime(2026, 4, 1, 16, 0, tzinfo=UTC)
        candidate = _make_existing_event("Soccer Practice", dt, child_ids=[child_a])

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [candidate]
        mock_session.execute.return_value = mock_result

        result = await find_duplicate_event(
            mock_session, family_id, "Soccer Practice", dt,
            child_ids=[child_a],
        )
        assert result is not None
        assert result.id == candidate.id

    @pytest.mark.asyncio
    @patch("src.state.events.select")
    async def test_no_child_ids_ignores_child_filter(self, _mock_select, mock_session, family_id):
        """Without child_ids param, dedup works as before (ignores children)."""
        from src.state.events import find_duplicate_event

        dt = datetime(2026, 4, 1, 16, 0, tzinfo=UTC)
        candidate = _make_existing_event("Soccer Practice", dt, child_ids=[uuid4()])

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [candidate]
        mock_session.execute.return_value = mock_result

        result = await find_duplicate_event(
            mock_session, family_id, "Soccer Practice", dt,
        )
        assert result is not None


# ── _fuzzy_match_caregiver ──────────────────────────────────────────────


class TestFuzzyMatchCaregiver:
    def test_exact_match(self):
        caregivers = [_make_caregiver("Sarah"), _make_caregiver("Mike")]
        assert _fuzzy_match_caregiver("Sarah", caregivers).name == "Sarah"

    def test_case_insensitive(self):
        caregivers = [_make_caregiver("Sarah")]
        assert _fuzzy_match_caregiver("sarah", caregivers).name == "Sarah"
        assert _fuzzy_match_caregiver("SARAH", caregivers).name == "Sarah"

    def test_prefix_match(self):
        caregivers = [_make_caregiver("Sarah Thompson")]
        assert _fuzzy_match_caregiver("Sarah", caregivers).name == "Sarah Thompson"

    def test_reverse_prefix_match(self):
        """Input is longer than caregiver name — caregiver name is a prefix of input."""
        caregivers = [_make_caregiver("Sarah")]
        assert _fuzzy_match_caregiver("Sarah Thompson", caregivers).name == "Sarah"

    def test_no_match(self):
        caregivers = [_make_caregiver("Sarah"), _make_caregiver("Mike")]
        assert _fuzzy_match_caregiver("Jessica", caregivers) is None

    def test_empty_string(self):
        caregivers = [_make_caregiver("Sarah")]
        assert _fuzzy_match_caregiver("", caregivers) is None

    def test_whitespace_stripped(self):
        caregivers = [_make_caregiver("Sarah")]
        assert _fuzzy_match_caregiver("  Sarah  ", caregivers).name == "Sarah"


# ── Caregiver conflict detection ────────────────────────────────────────


class TestCaregiverConflictDetection:
    @pytest.mark.asyncio
    @patch("src.agents.calendar.events_dal")
    async def test_caregiver_double_book(self, mock_events_dal, mock_session, family_id):
        cg_id = uuid4()
        dt = datetime(2026, 4, 1, 10, 0, tzinfo=UTC)

        existing = _make_existing_event(
            "Dentist Appointment", dt, caregiver_ids=[cg_id], location="Dental Clinic"
        )
        mock_events_dal.get_events_in_range = AsyncMock(return_value=[existing])

        new_event = ResolvedEvent(
            title="Work Lunch",
            datetime_start=dt + timedelta(minutes=30),
            datetime_end=dt + timedelta(hours=1, minutes=30),
            caregiver_ids=[cg_id],
            location="Downtown",
        )

        conflicts = await detect_conflicts(mock_session, family_id, new_event)

        assert len(conflicts) >= 1
        # Different locations + caregiver overlap → location_impossible
        cg_conflicts = [
            c for c in conflicts
            if c.conflict_type in ("caregiver_double_book", "location_impossible")
        ]
        assert len(cg_conflicts) >= 1

    @pytest.mark.asyncio
    @patch("src.agents.calendar.events_dal")
    async def test_no_conflict_different_caregiver(self, mock_events_dal, mock_session, family_id):
        dt = datetime(2026, 4, 1, 10, 0, tzinfo=UTC)

        existing = _make_existing_event(
            "Dentist", dt, caregiver_ids=[uuid4()]
        )
        mock_events_dal.get_events_in_range = AsyncMock(return_value=[existing])

        new_event = ResolvedEvent(
            title="Work Lunch",
            datetime_start=dt,
            caregiver_ids=[uuid4()],  # Different caregiver
        )

        conflicts = await detect_conflicts(mock_session, family_id, new_event)
        cg_conflicts = [c for c in conflicts if c.conflict_type == "caregiver_double_book"]
        assert len(cg_conflicts) == 0

    @pytest.mark.asyncio
    @patch("src.agents.calendar.events_dal")
    async def test_no_conflict_non_overlapping(self, mock_events_dal, mock_session, family_id):
        cg_id = uuid4()
        dt = datetime(2026, 4, 1, 10, 0, tzinfo=UTC)

        existing = _make_existing_event("Dentist", dt, caregiver_ids=[cg_id])
        existing.datetime_end = dt + timedelta(hours=1)
        mock_events_dal.get_events_in_range = AsyncMock(return_value=[existing])

        new_event = ResolvedEvent(
            title="Lunch",
            datetime_start=dt + timedelta(hours=3),
            caregiver_ids=[cg_id],
        )

        conflicts = await detect_conflicts(mock_session, family_id, new_event)
        assert len(conflicts) == 0


# ── Schema fields ───────────────────────────────────────────────────────


class TestSchemaAttendeeFields:
    def test_extracted_event_has_caregiver_names(self):
        evt = ExtractedEvent(
            title="Work Dinner",
            date_str="2026-04-01",
            caregiver_names=["Sarah"],
        )
        assert evt.caregiver_names == ["Sarah"]

    def test_extracted_event_caregiver_names_default_empty(self):
        evt = ExtractedEvent(title="Soccer", date_str="2026-04-01")
        assert evt.caregiver_names == []

    def test_email_extracted_event_has_caregiver_names(self):
        evt = EmailExtractedEvent(
            title="Parent Night",
            date_str="2026-04-01",
            caregiver_names=["Mike", "Sarah"],
        )
        assert evt.caregiver_names == ["Mike", "Sarah"]

    def test_resolved_event_has_caregiver_ids(self):
        cg1, cg2 = uuid4(), uuid4()
        evt = ResolvedEvent(
            title="Lunch",
            datetime_start=datetime(2026, 4, 1, 12, 0, tzinfo=UTC),
            caregiver_ids=[cg1, cg2],
        )
        assert evt.caregiver_ids == [cg1, cg2]

    def test_resolved_event_caregiver_ids_default_empty(self):
        evt = ResolvedEvent(
            title="Lunch",
            datetime_start=datetime(2026, 4, 1, 12, 0, tzinfo=UTC),
        )
        assert evt.caregiver_ids == []

    def test_extracted_update_has_new_child_names(self):
        upd = ExtractedUpdate(
            target_event_hint="soccer",
            new_child_names=["Emma", "Jake"],
        )
        assert upd.new_child_names == ["Emma", "Jake"]

    def test_extracted_update_has_new_caregiver_names(self):
        upd = ExtractedUpdate(
            target_event_hint="dinner",
            new_caregiver_names=["Sarah"],
        )
        assert upd.new_caregiver_names == ["Sarah"]

    def test_extracted_update_attendee_fields_default_none(self):
        upd = ExtractedUpdate(target_event_hint="soccer")
        assert upd.new_child_names is None
        assert upd.new_caregiver_names is None
