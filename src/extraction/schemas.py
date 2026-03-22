"""Pydantic schemas for extraction pipeline: intents, events, action items."""

import enum
from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, Field

# ── Intent classification ───────────────────────────────────────────────


class IntentType(enum.StrEnum):
    """Classified intent types for WhatsApp messages."""

    add_event = "add_event"
    query_schedule = "query_schedule"
    modify_event = "modify_event"
    cancel_event = "cancel_event"
    assign_transport = "assign_transport"
    release_transport = "release_transport"
    rsvp_response = "rsvp_response"
    share_info = "share_info"
    approval_response = "approval_response"  # approve/dismiss/edit pending action
    event_update = "event_update"  # update info about an existing event (mark task done, add notes)
    set_preference = "set_preference"  # caregiver states a preference ("don't message before 7am")
    correct_learning = "correct_learning"  # correcting a fact/preference ("actually Emma goes to Washington Elementary")
    general_question = "general_question"
    greeting = "greeting"
    unknown = "unknown"


class IntentResult(BaseModel):
    """Result of intent classification."""

    intent: IntentType
    confidence: float = Field(ge=0.0, le=1.0)
    extracted_params: dict = Field(default_factory=dict)
    pending_action_id: UUID | None = None  # Set when responding to a pending action


# ── Extracted data schemas ──────────────────────────────────────────────


class ExtractedEvent(BaseModel):
    """Structured event data extracted from text."""

    title: str
    event_type: str = "other"
    datetime_start: datetime | None = None
    datetime_end: datetime | None = None
    date_text: str | None = None  # Raw date text if parsing fails
    location: str | None = None
    description: str | None = None
    child_names: list[str] = Field(default_factory=list)
    rsvp_needed: bool = False
    rsvp_deadline: datetime | None = None
    rsvp_contact: str | None = None
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    time_explicit: bool = Field(
        default=False,
        description="True if the user explicitly stated a specific time (e.g., '3pm', 'at 10:00', 'noon'). "
        "False if time was inferred from vague terms like 'morning', 'evening', 'afternoon', or not mentioned.",
    )

    # Recurrence fields
    is_recurring: bool = Field(
        default=False,
        description="True if the event is explicitly recurring (e.g., 'every Monday', 'weekly', 'biweekly').",
    )
    recurrence_pattern: str | None = Field(
        default=None,
        description="Human-readable recurrence pattern (e.g., 'every Monday and Wednesday').",
    )
    recurrence_freq: str | None = Field(
        default=None,
        description="Recurrence frequency: WEEKLY, MONTHLY, or DAILY.",
    )
    recurrence_days: list[str] = Field(
        default_factory=list,
        description="Days of the week for recurrence using 2-letter codes: MO, TU, WE, TH, FR, SA, SU.",
    )
    recurrence_until: datetime | None = Field(
        default=None,
        description="End date for recurrence. None = indefinite.",
    )
    recurrence_interval: int = Field(
        default=1,
        description="Interval for recurrence. 2 = biweekly for WEEKLY freq.",
    )


class ExtractedActionItem(BaseModel):
    """Structured action item extracted from text."""

    description: str
    action_type: str = "other"
    due_date: datetime | None = None
    child_names: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class ExtractedRecurringPattern(BaseModel):
    """A recurring schedule pattern extracted from text."""

    activity_name: str
    activity_type: str = "other"
    pattern: str  # e.g. "Tuesdays and Thursdays 4-5pm"
    location: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    child_names: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class ExtractionResult(BaseModel):
    """Combined extraction result from an email or message."""

    events: list[ExtractedEvent] = Field(default_factory=list)
    action_items: list[ExtractedActionItem] = Field(default_factory=list)
    recurring_patterns: list[ExtractedRecurringPattern] = Field(default_factory=list)
    is_relevant: bool = True
    relevance_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    summary: str | None = None
