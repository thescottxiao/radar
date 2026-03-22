"""Shared timezone utilities for family-aware date/time calculations.

Any code that computes "today", "this week", or date boundaries for user-facing
features should use these helpers with the family's timezone — never bare
datetime.now() or datetime.now(UTC) for user-facing date boundaries.

UTC is still correct for: GCal API calls, watch expiry, audit timestamps,
absolute durations (pending action expiry), and DB TIMESTAMPTZ storage.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo


def get_family_now(family_timezone: str) -> datetime:
    """Get current datetime in the family's timezone (timezone-aware).

    Use this when computing date boundaries like "start of today" or
    "end of this week" for user-facing features.
    """
    return datetime.now(ZoneInfo(family_timezone))


def get_family_today(family_timezone: str) -> date:
    """Get today's date in the family's timezone.

    Use this instead of date.today() when the result affects user-visible
    behavior (schedule queries, digest boundaries, recurring schedule filters).
    """
    return get_family_now(family_timezone).date()
