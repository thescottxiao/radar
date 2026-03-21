"""Shared Pydantic schemas used by reasoning agents."""

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, Field

# ── Event extraction from natural language ──────────────────────────────


class ExtractedEvent(BaseModel):
    """Event details extracted from a natural language message."""

    title: str = Field(description="Short event title, e.g. 'Soccer Practice'")
    event_type: str = Field(
        default="other",
        description="One of: birthday_party, sports_practice, sports_game, "
        "school_event, camp, playdate, medical_appointment, "
        "dental_appointment, recital_performance, registration_deadline, other",
    )
    date_str: str = Field(
        description="Date string extracted from the message, e.g. 'next Tuesday', '2026-03-25'"
    )
    time_str: str | None = Field(
        default=None,
        description="Time string extracted, e.g. '4pm', '16:00'",
    )
    end_time_str: str | None = Field(
        default=None,
        description="End time if mentioned, e.g. '5:30pm'",
    )
    location: str | None = Field(default=None, description="Event location if mentioned")
    child_names: list[str] = Field(
        default_factory=list,
        description="Names of children this event is for",
    )
    description: str | None = Field(default=None, description="Additional details")
    is_recurring: bool = Field(default=False, description="Whether this is a recurring event")
    recurrence_pattern: str | None = Field(
        default=None,
        description="Recurrence pattern if recurring, e.g. 'every Tuesday'",
    )


class ResolvedEvent(BaseModel):
    """Event with resolved datetime values, ready for DB insertion."""

    title: str
    event_type: str = "other"
    datetime_start: datetime
    datetime_end: datetime | None = None
    location: str | None = None
    child_ids: list[UUID] = Field(default_factory=list)
    description: str | None = None
    is_recurring: bool = False
    recurrence_pattern: str | None = None


# ── Conflict detection ──────────────────────────────────────────────────


class Conflict(BaseModel):
    """A detected scheduling conflict."""

    existing_event_id: UUID
    existing_event_title: str
    existing_event_start: datetime
    existing_event_end: datetime | None = None
    existing_event_location: str | None = None
    conflict_type: str = Field(
        description="One of: time_overlap, child_double_book, location_impossible"
    )
    description: str = Field(description="Human-readable conflict description")
    child_names: list[str] = Field(
        default_factory=list,
        description="Children involved in the conflict",
    )


# ── Event update extraction ─────────────────────────────────────────────


class ExtractedUpdate(BaseModel):
    """Update details extracted from a natural language message."""

    target_event_hint: str = Field(
        description="Text identifying the event to update, e.g. 'soccer practice', 'the birthday party'"
    )
    new_date_str: str | None = Field(default=None, description="New date if changed")
    new_time_str: str | None = Field(default=None, description="New time if changed")
    new_end_time_str: str | None = Field(default=None, description="New end time if changed")
    new_location: str | None = Field(default=None, description="New location if changed")
    new_title: str | None = Field(default=None, description="New title if changed")
    additional_notes: str | None = Field(default=None)


# ── Correction extraction ───────────────────────────────────────────────


class ExtractedCorrection(BaseModel):
    """Correction details for a recently mentioned event."""

    target_event_hint: str = Field(
        description="Text identifying the event to correct"
    )
    corrected_date_str: str | None = Field(default=None)
    corrected_time_str: str | None = Field(default=None)
    corrected_location: str | None = Field(default=None)
    corrected_title: str | None = Field(default=None)


# ── Onboarding extraction ──────────────────────────────────────────────


class ExtractedChild(BaseModel):
    """Child info extracted from onboarding message."""

    name: str = Field(description="Child's first name")
    age: int | None = Field(default=None, description="Child's age if mentioned")
    date_of_birth: date | None = Field(default=None, description="Date of birth if mentioned")
    activities: list[str] = Field(
        default_factory=list,
        description="Activities/sports/hobbies mentioned",
    )


class OnboardingExtraction(BaseModel):
    """Children info extracted from an onboarding message."""

    children: list[ExtractedChild] = Field(
        default_factory=list,
        description="List of children mentioned by the caregiver",
    )
    caregiver_name: str | None = Field(
        default=None,
        description="The caregiver's name if they mentioned it",
    )


# ── Assignment extraction ──────────────────────────────────────────────


class ExtractedAssignment(BaseModel):
    """Transport assignment extracted from message."""

    child_name: str = Field(description="Name of child being claimed for transport")
    event_hint: str | None = Field(
        default=None,
        description=(
            "Which event this assignment is for. Extract from the user's message "
            "OR from recent conversation context. If the recent conversation was about "
            "a specific event (e.g., 'soccer practice'), set this to that event name "
            "even if the current message just says 'handle it'."
        ),
    )
    role: str = Field(
        default="both",
        description="One of: drop_off, pick_up, both",
    )
    assigned_caregiver: str | None = Field(
        default=None,
        description=(
            "Name of the caregiver being assigned. If the sender says "
            "'I'll handle it' this is null (meaning the sender). If they say "
            "'Nick has dropoff' or 'Dad is doing pickup', this is 'Nick' or 'Dad'."
        ),
    )


class ExtractedRelease(BaseModel):
    """Transport release extracted from message (caregiver can't cover an assignment)."""

    child_name: str | None = Field(
        default=None,
        description="Name of child, if mentioned",
    )
    event_hint: str | None = Field(
        default=None,
        description="Which event this release is for, if mentioned",
    )
    role: str = Field(
        default="both",
        description="One of: drop_off, pick_up, both",
    )


# ── Calendar query context ─────────────────────────────────────────────


class CalendarQueryContext(BaseModel):
    """Context for answering calendar queries."""

    family_timezone: str = "America/New_York"
    children_names: list[str] = Field(default_factory=list)
    caregiver_names: list[str] = Field(default_factory=list)
    today: str = Field(description="Today's date as YYYY-MM-DD")
