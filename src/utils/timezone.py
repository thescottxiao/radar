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


# ── Display formatting ──────────────────────────────────────────────────

# Default format: "Tue Mar 31, 05:30 PM"
FMT_DATETIME = "%a %b %d, %I:%M %p"
# Time only: "05:30 PM"
FMT_TIME = "%I:%M %p"
# Date only: "Mar 31"
FMT_DATE_SHORT = "%b %d"
# Long day+date+time: "Tuesday, March 31 at 05:30 PM"
FMT_DATETIME_LONG = "%A, %B %d at %I:%M %p"


def to_local(dt: datetime, family_timezone: str) -> datetime:
    """Convert a datetime to the family's local timezone.

    Handles both timezone-aware and naive datetimes:
    - Aware datetimes are converted via astimezone()
    - Naive datetimes are assumed UTC and then converted

    Use this before any user-facing strftime() call.
    """
    tz = ZoneInfo(family_timezone)
    if dt.tzinfo is None:
        from datetime import UTC
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(tz)


def fmt_dt(dt: datetime | None, family_timezone: str, fmt: str = FMT_DATETIME) -> str:
    """Format a datetime for user display in the family's local timezone.

    This is the ONE function all user-facing datetime display should use.
    Converts to local timezone, then formats.

    Returns "TBD" if dt is None.

    Common formats (importable constants):
        FMT_DATETIME      "Tue Mar 31, 05:30 PM"  (default)
        FMT_TIME          "05:30 PM"
        FMT_DATE_SHORT    "Mar 31"
        FMT_DATETIME_LONG "Tuesday, March 31 at 05:30 PM"
    """
    if dt is None:
        return "TBD"
    return to_local(dt, family_timezone).strftime(fmt)


def fmt_event_time(event, family_tz: str, timed_fmt: str = FMT_DATETIME) -> str:
    """Format event time, handling all-day, time-TBD, and estimated time events."""
    if getattr(event, 'all_day', False):
        return fmt_dt(event.datetime_start, family_tz, fmt="%b %d") + " (all day)"
    if getattr(event, 'time_tbd', False):
        return fmt_dt(event.datetime_start, family_tz, fmt="%b %d") + " (time TBD)"
    if not getattr(event, 'time_explicit', True):
        return fmt_dt(event.datetime_start, family_tz, fmt=timed_fmt) + " (est.)"
    return fmt_dt(event.datetime_start, family_tz, fmt=timed_fmt)
