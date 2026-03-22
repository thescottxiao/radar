import enum
from datetime import date, datetime, time
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    SmallInteger,
    Text,
    Time,
    text,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import BYTEA, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


# Forward-declare enums so we can reference them in type_annotation_map
# (actual definitions below)


class Base(DeclarativeBase):
    pass


# ── Enum types matching schema.sql ──────────────────────────────────────


class EventSource(enum.StrEnum):
    email = "email"
    calendar = "calendar"
    manual = "manual"
    ics_feed = "ics_feed"
    forwarded = "forwarded"


class RsvpStatus(enum.StrEnum):
    pending = "pending"
    accepted = "accepted"
    declined = "declined"
    not_applicable = "not_applicable"


class RsvpMethod(enum.StrEnum):
    reply_email = "reply_email"
    click_link = "click_link"
    form = "form"
    phone = "phone"
    not_applicable = "not_applicable"


class ActivityType(enum.StrEnum):
    sport = "sport"
    music = "music"
    academic = "academic"
    social = "social"
    medical = "medical"
    other = "other"


class TaskStatus(enum.StrEnum):
    pending = "pending"
    complete = "complete"


class ActionItemType(enum.StrEnum):
    form_to_sign = "form_to_sign"
    payment_due = "payment_due"
    item_to_bring = "item_to_bring"
    item_to_purchase = "item_to_purchase"
    registration_deadline = "registration_deadline"
    rsvp_needed = "rsvp_needed"
    contact_needed = "contact_needed"
    other = "other"


class ActionItemStatus(enum.StrEnum):
    pending = "pending"
    complete = "complete"
    dismissed = "dismissed"


class GcalOutboxOperation(enum.StrEnum):
    create = "create"
    update = "update"
    patch = "patch"
    delete = "delete"


class GcalOutboxStatus(enum.StrEnum):
    pending = "pending"
    processing = "processing"
    done = "done"
    failed = "failed"
    dead = "dead"


class PendingActionType(enum.StrEnum):
    rsvp_email = "rsvp_email"
    playdate_message = "playdate_message"
    coach_email = "coach_email"
    gift_selection = "gift_selection"
    camp_registration = "camp_registration"
    general_approval = "general_approval"
    event_confirmation = "event_confirmation"


class PendingActionStatus(enum.StrEnum):
    awaiting_approval = "awaiting_approval"
    approved = "approved"
    dismissed = "dismissed"
    expired = "expired"


# ── Map Python enums to Postgres enum type names ──────────────────────────
# SQLAlchemy defaults to lowercased class name (e.g. "pendingactionstatus")
# but schema.sql uses snake_case (e.g. "pending_action_status").

Base.registry.update_type_annotation_map({
    EventSource: SAEnum(EventSource, name="event_source", create_type=False),

    RsvpStatus: SAEnum(RsvpStatus, name="rsvp_status", create_type=False),
    RsvpMethod: SAEnum(RsvpMethod, name="rsvp_method", create_type=False),
    ActivityType: SAEnum(ActivityType, name="activity_type", create_type=False),
    TaskStatus: SAEnum(TaskStatus, name="task_status", create_type=False),
    ActionItemType: SAEnum(ActionItemType, name="action_item_type", create_type=False),
    ActionItemStatus: SAEnum(ActionItemStatus, name="action_item_status", create_type=False),
    PendingActionType: SAEnum(PendingActionType, name="pending_action_type", create_type=False),
    PendingActionStatus: SAEnum(PendingActionStatus, name="pending_action_status", create_type=False),
})


# ── Core tenant tables ──────────────────────────────────────────────────


class Family(Base):
    __tablename__ = "families"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    whatsapp_group_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    forward_email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    onboarding_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    timezone: Mapped[str] = mapped_column(Text, nullable=False, default="America/New_York")
    daily_digest_time: Mapped[time] = mapped_column(Time, nullable=False, default=time(7, 0))
    weekly_summary_day: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    weekly_summary_time: Mapped[time] = mapped_column(Time, nullable=False, default=time(9, 0))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("NOW()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("NOW()")
    )

    caregivers: Mapped[list["Caregiver"]] = relationship(back_populates="family", cascade="all, delete-orphan")
    children: Mapped[list["Child"]] = relationship(back_populates="family", cascade="all, delete-orphan")


class Caregiver(Base):
    __tablename__ = "caregivers"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    family_id: Mapped[UUID] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"), nullable=False)
    whatsapp_phone: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str | None] = mapped_column(Text)
    google_account_email: Mapped[str | None] = mapped_column(Text)
    google_refresh_token_encrypted: Mapped[bytes | None] = mapped_column(BYTEA)
    google_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    gmail_watch_history_id: Mapped[int | None] = mapped_column()
    gmail_watch_expiry: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    gcal_sync_token: Mapped[str | None] = mapped_column(Text)
    gcal_watch_channel_id: Mapped[str | None] = mapped_column(Text)
    gcal_watch_expiry: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("NOW()")
    )

    family: Mapped["Family"] = relationship(back_populates="caregivers")

    __table_args__ = (
        Index("idx_caregivers_family", "family_id"),
        Index("idx_caregivers_phone", "whatsapp_phone"),
        Index("idx_caregivers_email", "google_account_email"),
        {"extend_existing": True},
    )


# ── Family profile tables ──────────────────────────────────────────────


class Child(Base):
    __tablename__ = "children"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    family_id: Mapped[UUID] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    date_of_birth: Mapped[date | None] = mapped_column(Date)
    school: Mapped[str | None] = mapped_column(Text)
    grade: Mapped[str | None] = mapped_column(Text)
    activities: Mapped[list[str] | None] = mapped_column(ARRAY(Text), default=[])
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("NOW()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("NOW()")
    )

    family: Mapped["Family"] = relationship(back_populates="children")

    __table_args__ = (Index("idx_children_family", "family_id"),)


class ChildFriend(Base):
    __tablename__ = "child_friends"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    child_id: Mapped[UUID] = mapped_column(ForeignKey("children.id", ondelete="CASCADE"), nullable=False)
    family_id: Mapped[UUID] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"), nullable=False)
    friend_name: Mapped[str] = mapped_column(Text, nullable=False)
    parent_name: Mapped[str | None] = mapped_column(Text)
    parent_contact: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("NOW()")
    )

    __table_args__ = (Index("idx_child_friends_family", "family_id"),)


class CaregiverPreferences(Base):
    __tablename__ = "caregiver_preferences"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    caregiver_id: Mapped[UUID] = mapped_column(
        ForeignKey("caregivers.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    family_id: Mapped[UUID] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"), nullable=False)
    quiet_hours_start: Mapped[time | None] = mapped_column(Time)
    quiet_hours_end: Mapped[time | None] = mapped_column(Time)
    delegation_areas: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("NOW()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("NOW()")
    )

    __table_args__ = (Index("idx_caregiver_prefs_family", "family_id"),)


# ── Event system ────────────────────────────────────────────────────────


class RecurringSchedule(Base):
    __tablename__ = "recurring_schedules"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    family_id: Mapped[UUID] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"), nullable=False)
    child_id: Mapped[UUID | None] = mapped_column(ForeignKey("children.id", ondelete="SET NULL"))
    activity_name: Mapped[str] = mapped_column(Text, nullable=False)
    activity_type: Mapped[ActivityType] = mapped_column(default=ActivityType.other)
    pattern: Mapped[str] = mapped_column(Text, nullable=False)
    rrule: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date | None] = mapped_column(Date)
    confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    default_drop_off_caregiver: Mapped[UUID | None] = mapped_column(ForeignKey("caregivers.id"))
    default_pick_up_caregiver: Mapped[UUID | None] = mapped_column(ForeignKey("caregivers.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("NOW()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("NOW()")
    )

    __table_args__ = (
        Index("idx_recurring_schedules_family", "family_id"),
        Index("idx_recurring_schedules_child", "child_id"),
    )


class ScheduleException(Base):
    __tablename__ = "schedule_exceptions"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    recurring_schedule_id: Mapped[UUID] = mapped_column(
        ForeignKey("recurring_schedules.id", ondelete="CASCADE"), nullable=False
    )
    family_id: Mapped[UUID] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"), nullable=False)
    original_date: Mapped[date] = mapped_column(Date, nullable=False)
    exception_type: Mapped[str] = mapped_column(Text, nullable=False)
    new_date: Mapped[date | None] = mapped_column(Date)
    new_location: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("NOW()")
    )

    __table_args__ = (
        Index("idx_schedule_exceptions_schedule", "recurring_schedule_id"),
        Index("idx_schedule_exceptions_family", "family_id"),
    )


class Event(Base):
    __tablename__ = "events"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    family_id: Mapped[UUID] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"), nullable=False)
    source: Mapped[EventSource] = mapped_column(nullable=False)
    source_refs: Mapped[list[str] | None] = mapped_column(ARRAY(Text), default=[])
    type: Mapped[str] = mapped_column(Text, nullable=False, default="other")
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    datetime_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    datetime_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    location: Mapped[str | None] = mapped_column(Text)

    # Recurrence
    is_recurring: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    recurring_schedule_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("recurring_schedules.id", ondelete="SET NULL")
    )
    rrule: Mapped[str | None] = mapped_column(Text)

    # RSVP
    rsvp_status: Mapped[RsvpStatus] = mapped_column(nullable=False, default=RsvpStatus.not_applicable)
    rsvp_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rsvp_method: Mapped[RsvpMethod] = mapped_column(nullable=False, default=RsvpMethod.not_applicable)
    rsvp_contact: Mapped[str | None] = mapped_column(Text)

    # Transportation
    drop_off_by: Mapped[UUID | None] = mapped_column(ForeignKey("caregivers.id"))
    pick_up_by: Mapped[UUID | None] = mapped_column(ForeignKey("caregivers.id"))
    transport_notes: Mapped[str | None] = mapped_column(Text)

    # Metadata
    extraction_confidence: Mapped[float | None] = mapped_column(Float)
    confirmed_by_caregiver: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("NOW()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("NOW()")
    )

    children: Mapped[list["EventChild"]] = relationship(cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_events_family", "family_id"),
        Index("idx_events_datetime", "family_id", "datetime_start"),
        Index("idx_events_recurring", "recurring_schedule_id"),
        Index("idx_events_type", "family_id", "type"),
    )


class EventChild(Base):
    __tablename__ = "event_children"

    event_id: Mapped[UUID] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), primary_key=True
    )
    child_id: Mapped[UUID] = mapped_column(
        ForeignKey("children.id", ondelete="CASCADE"), primary_key=True
    )
    family_id: Mapped[UUID] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"), nullable=False)

    __table_args__ = (Index("idx_event_children_family", "family_id"),)


class PrepTask(Base):
    __tablename__ = "prep_tasks"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    event_id: Mapped[UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    family_id: Mapped[UUID] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"), nullable=False)
    task: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[TaskStatus] = mapped_column(nullable=False, default=TaskStatus.pending)
    assigned_to: Mapped[UUID | None] = mapped_column(ForeignKey("caregivers.id"))
    due_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("NOW()")
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("idx_prep_tasks_event", "event_id"),
        Index("idx_prep_tasks_family", "family_id"),
    )


# ── Action items ────────────────────────────────────────────────────────


class ActionItem(Base):
    __tablename__ = "action_items"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    family_id: Mapped[UUID] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"), nullable=False)
    event_id: Mapped[UUID | None] = mapped_column(ForeignKey("events.id", ondelete="SET NULL"))
    source: Mapped[EventSource] = mapped_column(nullable=False)
    source_ref: Mapped[str | None] = mapped_column(Text)
    type: Mapped[ActionItemType] = mapped_column(nullable=False, default=ActionItemType.other)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    due_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[ActionItemStatus] = mapped_column(nullable=False, default=ActionItemStatus.pending)
    assigned_to: Mapped[UUID | None] = mapped_column(ForeignKey("caregivers.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("NOW()")
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    children: Mapped[list["ActionItemChild"]] = relationship(cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_action_items_family", "family_id"),
        Index("idx_action_items_status", "family_id", "status"),
        Index("idx_action_items_due", "family_id", "due_date"),
    )


class ActionItemChild(Base):
    __tablename__ = "action_item_children"

    action_item_id: Mapped[UUID] = mapped_column(
        ForeignKey("action_items.id", ondelete="CASCADE"), primary_key=True
    )
    child_id: Mapped[UUID] = mapped_column(
        ForeignKey("children.id", ondelete="CASCADE"), primary_key=True
    )
    family_id: Mapped[UUID] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"), nullable=False)


# ── Family learning & preferences ──────────────────────────────────────
# This table serves two roles:
#   1. Staging area for factual observations that graduate to structured tables
#   2. Permanent store for freeform preferences (pref_* categories) injected into LLM prompts


class FamilyLearning(Base):
    __tablename__ = "family_learnings"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    family_id: Mapped[UUID] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"), nullable=False)
    caregiver_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("caregivers.id", ondelete="SET NULL")
    )  # NULL = family-wide, set = per-caregiver
    category: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str | None] = mapped_column(Text)
    entity_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    fact: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    surfaced_in_summary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    graduated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    superseded_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("family_learnings.id")
    )  # points to replacement on correction
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("NOW()")
    )

    __table_args__ = (
        Index("idx_learnings_family", "family_id"),
    )


# ── Conversation memory ─────────────────────────────────────────────────


class ConversationMemory(Base):
    __tablename__ = "conversation_memory"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    family_id: Mapped[UUID] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"), nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding = mapped_column(Vector(1536), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("NOW()")
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("idx_memory_family", "family_id"),)


# ── ICS subscriptions ───────────────────────────────────────────────────


class IcsSubscription(Base):
    __tablename__ = "ics_subscriptions"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    family_id: Mapped[UUID] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str | None] = mapped_column(Text)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_etag: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("NOW()")
    )

    __table_args__ = (Index("idx_ics_family", "family_id"),)


# ── Pending actions ─────────────────────────────────────────────────────


class PendingAction(Base):
    __tablename__ = "pending_actions"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    family_id: Mapped[UUID] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"), nullable=False)
    type: Mapped[PendingActionType] = mapped_column(nullable=False)
    status: Mapped[PendingActionStatus] = mapped_column(
        nullable=False, default=PendingActionStatus.awaiting_approval
    )
    draft_content: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    initiated_by: Mapped[UUID | None] = mapped_column(ForeignKey("caregivers.id"))
    resolved_by: Mapped[UUID | None] = mapped_column(ForeignKey("caregivers.id"))
    edit_history: Mapped[list | None] = mapped_column(ARRAY(JSONB), default=[])
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("NOW()")
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("idx_pending_family", "family_id"),
    )


# ── Sent emails audit ───────────────────────────────────────────────────


class SentEmail(Base):
    __tablename__ = "sent_emails"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    family_id: Mapped[UUID] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"), nullable=False)
    pending_action_id: Mapped[UUID | None] = mapped_column(ForeignKey("pending_actions.id"))
    from_address: Mapped[str] = mapped_column(Text, nullable=False)
    to_address: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    approved_by: Mapped[UUID] = mapped_column(ForeignKey("caregivers.id"), nullable=False)
    edit_history: Mapped[list | None] = mapped_column(ARRAY(JSONB), default=[])
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("NOW()")
    )
    delivery_status: Mapped[str | None] = mapped_column(Text, default="sent")
    delivery_provider_id: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("idx_sent_emails_family", "family_id"),)


# ── Extraction feedback ─────────────────────────────────────────────────


class ExtractionFeedback(Base):
    __tablename__ = "extraction_feedback"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    family_id: Mapped[UUID] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"), nullable=False)
    raw_email_hash: Mapped[str] = mapped_column(Text, nullable=False)
    original_extraction: Mapped[dict] = mapped_column(JSONB, nullable=False)
    corrected_extraction: Mapped[dict] = mapped_column(JSONB, nullable=False)
    correction_type: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("NOW()")
    )

    __table_args__ = (Index("idx_feedback_family", "family_id"),)


# ── GCal outbox ────────────────────────────────────────────────────────


class GcalOutboxItem(Base):
    __tablename__ = "gcal_outbox"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    family_id: Mapped[UUID] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"), nullable=False)
    event_id: Mapped[UUID | None] = mapped_column(ForeignKey("events.id", ondelete="SET NULL"))
    operation: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    gcal_event_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default=GcalOutboxStatus.pending.value)
    retry_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    max_retries: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=5)
    last_error: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("NOW()")
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_retry_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("NOW()")
    )

    __table_args__ = (
        Index("idx_gcal_outbox_family", "family_id"),
        Index("idx_gcal_outbox_pending", "status", "next_retry_at"),
    )
