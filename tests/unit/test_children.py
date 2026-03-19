import os

import pytest
from src.state import children as child_dal

pytestmark = pytest.mark.skipif(
    os.environ.get("SKIP_DB_TESTS", "1") == "1", reason="Database not available"
)


class TestChildren:
    async def test_create_child(self, session, sample_family):
        family = sample_family["family"]
        child = await child_dal.create_child(session, family.id, "Oliver")
        assert child.id is not None
        assert child.name == "Oliver"

    async def test_get_children_for_family(self, session, sample_family):
        family = sample_family["family"]
        children = await child_dal.get_children_for_family(session, family.id)
        assert len(children) == 2
        names = {c.name for c in children}
        assert names == {"Emma", "Jake"}

    async def test_fuzzy_match_exact(self, session, sample_family):
        family = sample_family["family"]
        match = await child_dal.fuzzy_match_child(session, family.id, "Emma")
        assert match is not None
        assert match.name == "Emma"

    async def test_fuzzy_match_case_insensitive(self, session, sample_family):
        family = sample_family["family"]
        match = await child_dal.fuzzy_match_child(session, family.id, "emma")
        assert match is not None
        assert match.name == "Emma"

    async def test_fuzzy_match_prefix(self, session, sample_family):
        family = sample_family["family"]
        match = await child_dal.fuzzy_match_child(session, family.id, "Em")
        assert match is not None
        assert match.name == "Emma"

    async def test_fuzzy_match_no_match(self, session, sample_family):
        family = sample_family["family"]
        match = await child_dal.fuzzy_match_child(session, family.id, "Oliver")
        assert match is None
