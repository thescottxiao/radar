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
    rsvp_response = "rsvp_response"
    add_child_info = "add_child_info"
    approval_response = "approval_response"  # approve/dismiss/edit pending action
    event_update = "event_update"  # update info about an existing event (mark task done, add notes)
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
