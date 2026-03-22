"""Detect recurring patterns from repeated single events.

After a new event is added, checks if 3+ similar events exist on the same
day-of-week. If so, returns a suggestion to make it a recurring event.
"""

import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from src.state.events import compute_title_similarity
from src.state.models import Event

logger = logging.getLogger(__name__)

# Minimum number of similar events on the same day-of-week to suggest recurrence
MIN_EVENTS_FOR_PATTERN = 3


class RecurrenceCandidate:
    """A detected recurring pattern from individual events."""

    def __init__(
        self,
        activity_name: str,
        day_of_week: int,
        day_code: str,
        events: list[Event],
        suggested_rrule: str,
        human_pattern: str,
    ):
        self.activity_name = activity_name
        self.day_of_week = day_of_week
        self.day_code = day_code
        self.events = events
        self.suggested_rrule = suggested_rrule
        self.human_pattern = human_pattern


from src.utils.rrule import _DAY_NAMES

# Map Python weekday (0=Mon) to RRULE day code and name
_DOW_CODES = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
_DOW_TO_CODE = {i: code for i, code in enumerate(_DOW_CODES)}
_DOW_TO_NAME = {i: _DAY_NAMES[code] for i, code in enumerate(_DOW_CODES)}


async def detect_recurring_pattern(
    session: AsyncSession,
    family_id: UUID,
    new_event: Event,
) -> RecurrenceCandidate | None:
    """Check if a newly added event is part of a recurring pattern.

    Looks for 3+ events with similar titles on the same day of week
    within the last 60 days. Skips events already marked as recurring
    or cancelled.

    Returns a RecurrenceCandidate if a pattern is found, None otherwise.
    """
    from src.state import events as events_dal

    if not new_event.datetime_start:
        return None

    # Skip if this event is already recurring
    if new_event.is_recurring or new_event.recurring_schedule_id:
        return None

    # Get recent non-recurring events for this family
    now = datetime.now(UTC)
    start = now - timedelta(days=60)
    end = now + timedelta(days=30)
    all_events = await events_dal.get_events_in_range(session, family_id, start, end)

    # Filter to non-recurring, non-cancelled events with similar titles
    similar_events: list[Event] = []
    for ev in all_events:
        if ev.is_recurring or ev.recurring_schedule_id:
            continue
        if ev.description and "[CANCELLED]" in ev.description:
            continue
        if compute_title_similarity(new_event.title, ev.title) >= 0.7:
            similar_events.append(ev)

    if len(similar_events) < MIN_EVENTS_FOR_PATTERN:
        return None

    # Group by day of week
    by_dow: dict[int, list[Event]] = defaultdict(list)
    for ev in similar_events:
        if ev.datetime_start:
            by_dow[ev.datetime_start.weekday()].append(ev)

    # Find the day-of-week with the most events
    best_dow = max(by_dow, key=lambda d: len(by_dow[d]))
    dow_events = by_dow[best_dow]

    if len(dow_events) < MIN_EVENTS_FOR_PATTERN:
        return None

    day_code = _DOW_TO_CODE[best_dow]
    day_name = _DOW_TO_NAME[best_dow]

    from src.utils.rrule import build_rrule

    suggested_rrule = build_rrule("WEEKLY", byday=[day_code])
    human_pattern = f"every {day_name}"

    return RecurrenceCandidate(
        activity_name=new_event.title,
        day_of_week=best_dow,
        day_code=day_code,
        events=dow_events,
        suggested_rrule=suggested_rrule,
        human_pattern=human_pattern,
    )
