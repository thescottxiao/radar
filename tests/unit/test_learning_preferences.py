"""Tests for family learning & preferences system.

Tests the DAL functions, preference storage, graduation lifecycle,
and the shared context builder.
"""

import pytest
from datetime import time
from uuid import uuid4

from tests.conftest import requires_db


@requires_db
class TestLearningDAL:
    """Tests for src/state/learning.py functions."""

    async def test_create_learning_with_caregiver(self, session, sample_family):
        """Learning can be created with a caregiver_id for per-caregiver prefs."""
        from src.state import learning as learning_dal

        family = sample_family["family"]
        sarah = sample_family["sarah"]

        learning = await learning_dal.create_learning(
            session,
            family_id=family.id,
            category="pref_communication",
            fact="Keep messages short",
            caregiver_id=sarah.id,
            confirmed=True,
        )

        assert learning.caregiver_id == sarah.id
        assert learning.confirmed is True
        assert learning.graduated is False
        assert learning.superseded_by is None

    async def test_create_learning_family_wide(self, session, sample_family):
        """Learning with no caregiver_id is family-wide."""
        from src.state import learning as learning_dal

        family = sample_family["family"]

        learning = await learning_dal.create_learning(
            session,
            family_id=family.id,
            category="pref_scheduling",
            fact="No activities on Sundays",
            confirmed=True,
        )

        assert learning.caregiver_id is None
        assert learning.category == "pref_scheduling"

    async def test_get_confirmed_learnings(self, session, sample_family):
        """get_confirmed_learnings returns only confirmed, non-graduated, non-superseded."""
        from src.state import learning as learning_dal

        family = sample_family["family"]

        # Create confirmed learning
        confirmed = await learning_dal.create_learning(
            session, family.id, "child_school", "Emma goes to Lincoln Elementary",
            confirmed=True,
        )

        # Create unconfirmed learning (should not appear)
        await learning_dal.create_learning(
            session, family.id, "child_activity", "Jake does karate",
            confirmed=False,
        )

        results = await learning_dal.get_confirmed_learnings(session, family.id)
        assert len(results) >= 1
        ids = [r.id for r in results]
        assert confirmed.id in ids

    async def test_get_active_preferences_family_wide(self, session, sample_family):
        """get_active_preferences returns confirmed pref_* learnings."""
        from src.state import learning as learning_dal

        family = sample_family["family"]

        await learning_dal.create_learning(
            session, family.id, "pref_decision", "Default gift budget $30",
            confirmed=True,
        )
        await learning_dal.create_learning(
            session, family.id, "child_school", "Emma goes to Lincoln",
            confirmed=True,
        )

        prefs = await learning_dal.get_active_preferences(session, family.id)
        categories = [p.category for p in prefs]
        assert "pref_decision" in categories
        assert "child_school" not in categories  # Not a preference

    async def test_get_active_preferences_with_caregiver(self, session, sample_family):
        """Per-caregiver preferences are included when caregiver_id is provided."""
        from src.state import learning as learning_dal

        family = sample_family["family"]
        sarah = sample_family["sarah"]

        # Family-wide pref
        await learning_dal.create_learning(
            session, family.id, "pref_scheduling", "No Sundays",
            confirmed=True,
        )
        # Sarah-specific pref
        await learning_dal.create_learning(
            session, family.id, "pref_communication", "Keep it brief",
            caregiver_id=sarah.id, confirmed=True,
        )

        prefs = await learning_dal.get_active_preferences(
            session, family.id, caregiver_id=sarah.id
        )
        facts = [p.fact for p in prefs]
        assert "No Sundays" in facts
        assert "Keep it brief" in facts

    async def test_supersede_learning(self, session, sample_family):
        """supersede_learning creates replacement and marks old as superseded."""
        from src.state import learning as learning_dal

        family = sample_family["family"]

        old = await learning_dal.create_learning(
            session, family.id, "child_school", "Emma goes to Lincoln Elementary",
            confirmed=True,
        )

        new = await learning_dal.supersede_learning(
            session, old.id, family.id, "Emma goes to Washington Elementary",
        )

        assert new.confirmed is True
        assert new.fact == "Emma goes to Washington Elementary"

        # Refresh old
        await session.refresh(old)
        assert old.superseded_by == new.id

    async def test_graduate_learning(self, session, sample_family):
        """graduate_learning marks the learning as graduated."""
        from src.state import learning as learning_dal

        family = sample_family["family"]

        learning = await learning_dal.create_learning(
            session, family.id, "child_school", "Emma goes to Lincoln",
            confirmed=True,
        )

        await learning_dal.graduate_learning(session, learning.id, family.id)
        await session.refresh(learning)
        assert learning.graduated is True

    async def test_auto_confirm_previously_surfaced(self, session, sample_family):
        """Auto-confirms learnings that were surfaced but not corrected."""
        from src.state import learning as learning_dal

        family = sample_family["family"]

        # Create a learning that was surfaced but not yet confirmed
        learning = await learning_dal.create_learning(
            session, family.id, "child_activity", "Emma does gymnastics",
        )
        # Simulate it being surfaced in a previous summary
        await learning_dal.mark_surfaced(session, family.id, [learning.id])

        confirmed_ids = await learning_dal.auto_confirm_previously_surfaced(
            session, family.id
        )

        assert learning.id in confirmed_ids
        await session.refresh(learning)
        assert learning.confirmed is True

    async def test_unsurfaced_excludes_graduated_and_superseded(self, session, sample_family):
        """get_unsurfaced_learnings excludes graduated and superseded entries."""
        from src.state import learning as learning_dal

        family = sample_family["family"]

        # Normal unsurfaced learning
        normal = await learning_dal.create_learning(
            session, family.id, "contact", "Coach Smith: coach@email.com",
        )

        # Graduated learning
        graduated = await learning_dal.create_learning(
            session, family.id, "child_school", "Jake goes to Maple Elementary",
        )
        await learning_dal.graduate_learning(session, graduated.id, family.id)

        unsurfaced = await learning_dal.get_unsurfaced_learnings(session, family.id)
        ids = [u.id for u in unsurfaced]
        assert normal.id in ids
        assert graduated.id not in ids


@requires_db
class TestPreferencesDAL:
    """Tests for src/state/preferences.py functions."""

    async def test_get_or_create_preferences(self, session, sample_family):
        """Creates a default preferences row if none exists."""
        from src.state import preferences as pref_dal

        sarah = sample_family["sarah"]
        family = sample_family["family"]

        prefs = await pref_dal.get_or_create_preferences(
            session, sarah.id, family.id
        )
        assert prefs.caregiver_id == sarah.id
        assert prefs.quiet_hours_start is None
        assert prefs.delegation_areas is None

    async def test_update_quiet_hours(self, session, sample_family):
        """Can set quiet hours."""
        from src.state import preferences as pref_dal

        sarah = sample_family["sarah"]
        family = sample_family["family"]

        prefs = await pref_dal.update_preference(
            session, sarah.id, family.id,
            quiet_hours_start=time(22, 0),
            quiet_hours_end=time(7, 0),
        )
        assert prefs.quiet_hours_start == time(22, 0)
        assert prefs.quiet_hours_end == time(7, 0)

    async def test_is_in_quiet_hours_overnight(self, session, sample_family):
        """Correctly detects overnight quiet hours (e.g., 22:00 - 07:00)."""
        from src.state import preferences as pref_dal

        sarah = sample_family["sarah"]
        family = sample_family["family"]

        await pref_dal.update_preference(
            session, sarah.id, family.id,
            quiet_hours_start=time(22, 0),
            quiet_hours_end=time(7, 0),
        )

        # 23:00 should be in quiet hours
        assert await pref_dal.is_in_quiet_hours(session, sarah.id, time(23, 0)) is True
        # 06:00 should be in quiet hours
        assert await pref_dal.is_in_quiet_hours(session, sarah.id, time(6, 0)) is True
        # 12:00 should NOT be in quiet hours
        assert await pref_dal.is_in_quiet_hours(session, sarah.id, time(12, 0)) is False

    async def test_update_delegation_areas(self, session, sample_family):
        """Can set delegation areas."""
        from src.state import preferences as pref_dal

        sarah = sample_family["sarah"]
        family = sample_family["family"]

        prefs = await pref_dal.update_preference(
            session, sarah.id, family.id,
            delegation_areas=["school", "medical"],
        )
        assert prefs.delegation_areas == ["school", "medical"]


@requires_db
class TestContextBuilder:
    """Tests for src/agents/context.py."""

    async def test_context_includes_learnings(self, session, sample_family):
        """Family context includes confirmed learnings in the context string."""
        from src.state import learning as learning_dal
        from src.agents.context import build_family_context

        family = sample_family["family"]

        await learning_dal.create_learning(
            session, family.id, "schedule_pattern",
            "Mom usually does Tuesday pickup",
            confirmed=True,
        )

        ctx = await build_family_context(session, family.id)
        assert "Mom usually does Tuesday pickup" in ctx["family_context"]
        assert len(ctx["learnings"]) >= 1

    async def test_context_includes_preferences(self, session, sample_family):
        """Family context includes active preferences."""
        from src.state import learning as learning_dal
        from src.agents.context import build_family_context

        family = sample_family["family"]

        await learning_dal.create_learning(
            session, family.id, "pref_scheduling",
            "No activities on Sundays",
            confirmed=True,
        )

        ctx = await build_family_context(session, family.id)
        assert "No activities on Sundays" in ctx["family_context"]
        assert len(ctx["preferences"]) >= 1

    async def test_context_excludes_unconfirmed(self, session, sample_family):
        """Unconfirmed learnings are NOT included in context."""
        from src.state import learning as learning_dal
        from src.agents.context import build_family_context

        family = sample_family["family"]

        await learning_dal.create_learning(
            session, family.id, "child_school",
            "Jake goes to Maple Elementary",
            confirmed=False,
        )

        ctx = await build_family_context(session, family.id)
        assert "Maple Elementary" not in ctx["family_context"]


class TestIntentTypes:
    """Tests for new intent types (no DB needed)."""

    def test_set_preference_intent_exists(self):
        from src.extraction.schemas import IntentType
        assert IntentType.set_preference == "set_preference"

    def test_correct_learning_intent_exists(self):
        from src.extraction.schemas import IntentType
        assert IntentType.correct_learning == "correct_learning"

    def test_all_intent_types_present(self):
        from src.extraction.schemas import IntentType
        names = [e.value for e in IntentType]
        assert "set_preference" in names
        assert "correct_learning" in names


class TestExtractedLearningCategories:
    """Tests for ExtractedLearning category field."""

    def test_pref_categories_accepted(self):
        from src.extraction.email import ExtractedLearning

        for cat in [
            "pref_communication", "pref_scheduling", "pref_notification",
            "pref_prep", "pref_delegation", "pref_decision",
        ]:
            learning = ExtractedLearning(category=cat, fact="test")
            assert learning.category == cat

    def test_fact_categories_accepted(self):
        from src.extraction.email import ExtractedLearning

        for cat in [
            "child_school", "child_activity", "child_friend",
            "contact", "gear", "schedule_pattern", "budget",
        ]:
            learning = ExtractedLearning(category=cat, fact="test")
            assert learning.category == cat
