-- Radar Database Schema
-- Version: 0.1.0
-- Engine: PostgreSQL 15+ with pgvector extension
--
-- IMPORTANT: Every table includes family_id for row-level tenant isolation.
-- All queries MUST filter by family_id. No exceptions.

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "vector";

-- ============================================================
-- Core tenant tables
-- ============================================================

CREATE TABLE families (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    whatsapp_group_id TEXT NOT NULL UNIQUE,
    forward_email   TEXT NOT NULL UNIQUE,  -- e.g. "family-{id}@radar.app"
    onboarding_complete BOOLEAN NOT NULL DEFAULT FALSE,
    timezone        TEXT NOT NULL DEFAULT 'America/New_York',
    daily_digest_time TIME NOT NULL DEFAULT '07:00',
    weekly_summary_day SMALLINT NOT NULL DEFAULT 0,  -- 0=Sunday
    weekly_summary_time TIME NOT NULL DEFAULT '09:00',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE caregivers (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    family_id       UUID NOT NULL REFERENCES families(id) ON DELETE CASCADE,
    whatsapp_phone  TEXT NOT NULL,
    name            TEXT,
    google_account_email TEXT,
    google_refresh_token_encrypted BYTEA,  -- AES-256 encrypted
    google_token_expires_at TIMESTAMPTZ,
    gmail_watch_history_id BIGINT,
    gmail_watch_expiry TIMESTAMPTZ,
    gcal_sync_token TEXT,
    gcal_watch_channel_id TEXT,
    gcal_watch_expiry TIMESTAMPTZ,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    joined_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (family_id, whatsapp_phone)
);

CREATE INDEX idx_caregivers_family ON caregivers(family_id);
CREATE INDEX idx_caregivers_phone ON caregivers(whatsapp_phone);
CREATE INDEX idx_caregivers_email ON caregivers(google_account_email);

-- ============================================================
-- Family profile tables
-- ============================================================

CREATE TABLE children (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    family_id       UUID NOT NULL REFERENCES families(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    date_of_birth   DATE,
    school          TEXT,
    grade           TEXT,
    activities      TEXT[] DEFAULT '{}',
    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_children_family ON children(family_id);

CREATE TABLE child_friends (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    child_id        UUID NOT NULL REFERENCES children(id) ON DELETE CASCADE,
    family_id       UUID NOT NULL REFERENCES families(id) ON DELETE CASCADE,
    friend_name     TEXT NOT NULL,
    parent_name     TEXT,
    parent_contact  TEXT,  -- email or phone
    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_child_friends_family ON child_friends(family_id);

CREATE TABLE gear_inventory (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    child_id        UUID NOT NULL REFERENCES children(id) ON DELETE CASCADE,
    family_id       UUID NOT NULL REFERENCES families(id) ON DELETE CASCADE,
    item            TEXT NOT NULL,
    size            TEXT,
    condition       TEXT,  -- good, needs_replacement, borrowed
    last_updated    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_gear_family ON gear_inventory(family_id);

-- ============================================================
-- Event system
-- ============================================================

CREATE TYPE event_source AS ENUM (
    'email', 'calendar', 'manual', 'ics_feed', 'forwarded'
);

CREATE TYPE event_type AS ENUM (
    'birthday_party', 'sports_practice', 'sports_game', 'school_event',
    'camp', 'playdate', 'medical_appointment', 'dental_appointment',
    'recital_performance', 'registration_deadline', 'other'
);

CREATE TYPE rsvp_status AS ENUM (
    'pending', 'accepted', 'declined', 'not_applicable'
);

CREATE TYPE rsvp_method AS ENUM (
    'reply_email', 'click_link', 'form', 'phone', 'not_applicable'
);

-- Generalized recurring schedule: sports seasons, music lessons, tutoring, swim, etc.
CREATE TYPE activity_type AS ENUM (
    'sport', 'music', 'academic', 'social', 'medical', 'other'
);

CREATE TABLE recurring_schedules (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    family_id       UUID NOT NULL REFERENCES families(id) ON DELETE CASCADE,
    child_id        UUID NOT NULL REFERENCES children(id) ON DELETE CASCADE,
    activity_name   TEXT NOT NULL,  -- e.g. "soccer", "piano lessons", "swim class"
    activity_type   activity_type NOT NULL DEFAULT 'other',
    pattern         TEXT NOT NULL,  -- human-readable, e.g. "every Wednesday, 3:30-4:30pm"
    location        TEXT,
    start_date      DATE NOT NULL,
    end_date        DATE NOT NULL,
    confirmed       BOOLEAN NOT NULL DEFAULT FALSE,
    default_drop_off_caregiver UUID REFERENCES caregivers(id),
    default_pick_up_caregiver UUID REFERENCES caregivers(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_recurring_schedules_family ON recurring_schedules(family_id);
CREATE INDEX idx_recurring_schedules_child ON recurring_schedules(child_id);

CREATE TABLE schedule_exceptions (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    recurring_schedule_id UUID NOT NULL REFERENCES recurring_schedules(id) ON DELETE CASCADE,
    family_id       UUID NOT NULL REFERENCES families(id) ON DELETE CASCADE,
    original_date   DATE NOT NULL,
    exception_type  TEXT NOT NULL CHECK (exception_type IN ('cancelled', 'rescheduled', 'location_change', 'makeup')),
    new_date        DATE,
    new_location    TEXT,
    reason          TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_schedule_exceptions_schedule ON schedule_exceptions(recurring_schedule_id);
CREATE INDEX idx_schedule_exceptions_family ON schedule_exceptions(family_id);

CREATE TABLE events (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    family_id       UUID NOT NULL REFERENCES families(id) ON DELETE CASCADE,
    source          event_source NOT NULL,
    source_refs     TEXT[] DEFAULT '{}',  -- dedup: list of source IDs that mapped here
    type            event_type NOT NULL DEFAULT 'other',
    title           TEXT NOT NULL,
    description     TEXT,
    datetime_start  TIMESTAMPTZ NOT NULL,
    datetime_end    TIMESTAMPTZ,
    location        TEXT,

    -- Recurrence
    is_recurring    BOOLEAN NOT NULL DEFAULT FALSE,
    recurring_schedule_id UUID REFERENCES recurring_schedules(id) ON DELETE SET NULL,

    -- RSVP
    rsvp_status     rsvp_status NOT NULL DEFAULT 'not_applicable',
    rsvp_deadline   TIMESTAMPTZ,
    rsvp_method     rsvp_method NOT NULL DEFAULT 'not_applicable',
    rsvp_contact    TEXT,

    -- Transportation
    drop_off_by     UUID REFERENCES caregivers(id),
    pick_up_by      UUID REFERENCES caregivers(id),
    transport_notes TEXT,

    -- Metadata
    extraction_confidence FLOAT,  -- 0.0-1.0, from Email Extraction Agent
    confirmed_by_caregiver BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_events_family ON events(family_id);
CREATE INDEX idx_events_datetime ON events(family_id, datetime_start);
CREATE INDEX idx_events_recurring ON events(recurring_schedule_id);
CREATE INDEX idx_events_type ON events(family_id, type);

-- Junction table: events <-> children
CREATE TABLE event_children (
    event_id        UUID NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    child_id        UUID NOT NULL REFERENCES children(id) ON DELETE CASCADE,
    family_id       UUID NOT NULL REFERENCES families(id) ON DELETE CASCADE,
    PRIMARY KEY (event_id, child_id)
);

CREATE INDEX idx_event_children_family ON event_children(family_id);

-- Preparation tasks linked to events
CREATE TYPE task_status AS ENUM ('pending', 'complete');

CREATE TABLE prep_tasks (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    event_id        UUID NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    family_id       UUID NOT NULL REFERENCES families(id) ON DELETE CASCADE,
    task            TEXT NOT NULL,
    status          task_status NOT NULL DEFAULT 'pending',
    assigned_to     UUID REFERENCES caregivers(id),
    due_date        TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ
);

CREATE INDEX idx_prep_tasks_event ON prep_tasks(event_id);
CREATE INDEX idx_prep_tasks_family ON prep_tasks(family_id);

-- ============================================================
-- Action items (non-event actionables from emails)
-- ============================================================

CREATE TYPE action_item_type AS ENUM (
    'form_to_sign', 'payment_due', 'item_to_bring', 'item_to_purchase',
    'registration_deadline', 'rsvp_needed', 'contact_needed', 'other'
);

CREATE TYPE action_item_status AS ENUM ('pending', 'complete', 'dismissed');

CREATE TABLE action_items (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    family_id       UUID NOT NULL REFERENCES families(id) ON DELETE CASCADE,
    event_id        UUID REFERENCES events(id) ON DELETE SET NULL,
    source          event_source NOT NULL,
    source_ref      TEXT,
    type            action_item_type NOT NULL DEFAULT 'other',
    description     TEXT NOT NULL,
    due_date        TIMESTAMPTZ,
    status          action_item_status NOT NULL DEFAULT 'pending',
    assigned_to     UUID REFERENCES caregivers(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ
);

CREATE INDEX idx_action_items_family ON action_items(family_id);
CREATE INDEX idx_action_items_status ON action_items(family_id, status);
CREATE INDEX idx_action_items_due ON action_items(family_id, due_date);

-- Junction table: action_items <-> children
CREATE TABLE action_item_children (
    action_item_id  UUID NOT NULL REFERENCES action_items(id) ON DELETE CASCADE,
    child_id        UUID NOT NULL REFERENCES children(id) ON DELETE CASCADE,
    family_id       UUID NOT NULL REFERENCES families(id) ON DELETE CASCADE,
    PRIMARY KEY (action_item_id, child_id)
);

-- ============================================================
-- Family learning (silent inference from emails/conversations)
-- ============================================================

CREATE TABLE family_learnings (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    family_id       UUID NOT NULL REFERENCES families(id) ON DELETE CASCADE,
    category        TEXT NOT NULL CHECK (category IN (
        'child_school', 'child_activity', 'child_friend', 'contact',
        'gear', 'preference', 'schedule_pattern', 'budget'
    )),
    entity_type     TEXT CHECK (entity_type IN ('child', 'caregiver', 'external_contact')),
    entity_id       UUID,  -- FK to children or caregivers, nullable
    fact            TEXT NOT NULL,
    source          TEXT,  -- e.g. "Extracted from email — Lincoln Elementary Newsletter, March 12"
    confidence      FLOAT NOT NULL DEFAULT 0.5,
    confirmed       BOOLEAN NOT NULL DEFAULT FALSE,
    surfaced_in_summary BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_learnings_family ON family_learnings(family_id);
CREATE INDEX idx_learnings_unsurfaced ON family_learnings(family_id, surfaced_in_summary) WHERE surfaced_in_summary = FALSE;

-- ============================================================
-- Conversation memory
-- ============================================================

CREATE TABLE conversation_memory (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    family_id       UUID NOT NULL REFERENCES families(id) ON DELETE CASCADE,
    type            TEXT NOT NULL CHECK (type IN ('short_term', 'long_term_summary')),
    content         TEXT NOT NULL,
    embedding       vector(1536),  -- for semantic retrieval
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ  -- short_term entries expire
);

CREATE INDEX idx_memory_family ON conversation_memory(family_id);
CREATE INDEX idx_memory_embedding ON conversation_memory USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- ============================================================
-- ICS feed subscriptions
-- ============================================================

CREATE TABLE ics_subscriptions (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    family_id       UUID NOT NULL REFERENCES families(id) ON DELETE CASCADE,
    url             TEXT NOT NULL,
    label           TEXT,  -- e.g. "Jake's soccer league"
    last_polled_at  TIMESTAMPTZ,
    last_etag       TEXT,  -- HTTP ETag for conditional fetching
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_ics_family ON ics_subscriptions(family_id);

-- ============================================================
-- Pending actions (open conversation state for SUGGEST mode)
-- ============================================================

CREATE TYPE pending_action_type AS ENUM (
    'rsvp_email', 'playdate_message', 'coach_email', 'gift_selection',
    'camp_registration', 'general_approval'
);

CREATE TYPE pending_action_status AS ENUM (
    'awaiting_approval', 'approved', 'dismissed', 'expired'
);

CREATE TABLE pending_actions (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    family_id       UUID NOT NULL REFERENCES families(id) ON DELETE CASCADE,
    type            pending_action_type NOT NULL,
    status          pending_action_status NOT NULL DEFAULT 'awaiting_approval',
    draft_content   TEXT NOT NULL,  -- the current draft (updated on edits)
    context         JSONB NOT NULL DEFAULT '{}',  -- metadata: recipient, event_id, etc.
    initiated_by    UUID REFERENCES caregivers(id),
    resolved_by     UUID REFERENCES caregivers(id),
    edit_history    JSONB[] DEFAULT '{}',  -- array of {instruction, previous_draft, timestamp}
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at     TIMESTAMPTZ,
    expires_at      TIMESTAMPTZ  -- auto-expire if no response
);

CREATE INDEX idx_pending_family ON pending_actions(family_id);
CREATE INDEX idx_pending_active ON pending_actions(family_id, status) WHERE status = 'awaiting_approval';

-- ============================================================
-- Sent email audit log (emails sent from Radar's domain)
-- ============================================================

CREATE TABLE sent_emails (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    family_id       UUID NOT NULL REFERENCES families(id) ON DELETE CASCADE,
    pending_action_id UUID REFERENCES pending_actions(id),
    from_address    TEXT NOT NULL,  -- Radar's send domain, e.g. "sarah@notifications.radar.app"
    to_address      TEXT NOT NULL,
    subject         TEXT NOT NULL,
    body            TEXT NOT NULL,
    approved_by     UUID NOT NULL REFERENCES caregivers(id),
    edit_history    JSONB[] DEFAULT '{}',  -- draft revisions before approval
    sent_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    delivery_status TEXT DEFAULT 'sent',  -- sent, delivered, bounced, failed
    delivery_provider_id TEXT  -- SendGrid/Postmark message ID for tracking
);

CREATE INDEX idx_sent_emails_family ON sent_emails(family_id);

-- ============================================================
-- Extraction feedback (for model improvement)
-- ============================================================

CREATE TABLE extraction_feedback (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    family_id       UUID NOT NULL REFERENCES families(id) ON DELETE CASCADE,
    raw_email_hash  TEXT NOT NULL,  -- hash of email, not content (we don't store raw emails)
    original_extraction JSONB NOT NULL,
    corrected_extraction JSONB NOT NULL,
    correction_type TEXT NOT NULL CHECK (correction_type IN (
        'wrong_date', 'wrong_child', 'wrong_type', 'false_positive',
        'missed_event', 'wrong_location', 'other'
    )),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_feedback_family ON extraction_feedback(family_id);

-- ============================================================
-- Row-Level Security (RLS)
-- ============================================================
-- Enable RLS on all tables. Application role must set
-- current_setting('app.current_family_id') before queries.

ALTER TABLE families ENABLE ROW LEVEL SECURITY;
ALTER TABLE caregivers ENABLE ROW LEVEL SECURITY;
ALTER TABLE children ENABLE ROW LEVEL SECURITY;
ALTER TABLE child_friends ENABLE ROW LEVEL SECURITY;
ALTER TABLE gear_inventory ENABLE ROW LEVEL SECURITY;
ALTER TABLE recurring_schedules ENABLE ROW LEVEL SECURITY;
ALTER TABLE schedule_exceptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE sent_emails ENABLE ROW LEVEL SECURITY;
ALTER TABLE events ENABLE ROW LEVEL SECURITY;
ALTER TABLE event_children ENABLE ROW LEVEL SECURITY;
ALTER TABLE prep_tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE action_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE action_item_children ENABLE ROW LEVEL SECURITY;
ALTER TABLE family_learnings ENABLE ROW LEVEL SECURITY;
ALTER TABLE conversation_memory ENABLE ROW LEVEL SECURITY;
ALTER TABLE ics_subscriptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE pending_actions ENABLE ROW LEVEL SECURITY;
ALTER TABLE extraction_feedback ENABLE ROW LEVEL SECURITY;

-- Create policies (example for events, repeat pattern for all tables)
CREATE POLICY tenant_isolation_events ON events
    USING (family_id = current_setting('app.current_family_id')::UUID);

CREATE POLICY tenant_isolation_caregivers ON caregivers
    USING (family_id = current_setting('app.current_family_id')::UUID);

CREATE POLICY tenant_isolation_children ON children
    USING (family_id = current_setting('app.current_family_id')::UUID);

CREATE POLICY tenant_isolation_child_friends ON child_friends
    USING (family_id = current_setting('app.current_family_id')::UUID);

CREATE POLICY tenant_isolation_gear ON gear_inventory
    USING (family_id = current_setting('app.current_family_id')::UUID);

CREATE POLICY tenant_isolation_recurring_schedules ON recurring_schedules
    USING (family_id = current_setting('app.current_family_id')::UUID);

CREATE POLICY tenant_isolation_schedule_exceptions ON schedule_exceptions
    USING (family_id = current_setting('app.current_family_id')::UUID);

CREATE POLICY tenant_isolation_sent_emails ON sent_emails
    USING (family_id = current_setting('app.current_family_id')::UUID);

CREATE POLICY tenant_isolation_event_children ON event_children
    USING (family_id = current_setting('app.current_family_id')::UUID);

CREATE POLICY tenant_isolation_prep_tasks ON prep_tasks
    USING (family_id = current_setting('app.current_family_id')::UUID);

CREATE POLICY tenant_isolation_action_items ON action_items
    USING (family_id = current_setting('app.current_family_id')::UUID);

CREATE POLICY tenant_isolation_action_item_children ON action_item_children
    USING (family_id = current_setting('app.current_family_id')::UUID);

CREATE POLICY tenant_isolation_learnings ON family_learnings
    USING (family_id = current_setting('app.current_family_id')::UUID);

CREATE POLICY tenant_isolation_memory ON conversation_memory
    USING (family_id = current_setting('app.current_family_id')::UUID);

CREATE POLICY tenant_isolation_ics ON ics_subscriptions
    USING (family_id = current_setting('app.current_family_id')::UUID);

CREATE POLICY tenant_isolation_pending ON pending_actions
    USING (family_id = current_setting('app.current_family_id')::UUID);

CREATE POLICY tenant_isolation_feedback ON extraction_feedback
    USING (family_id = current_setting('app.current_family_id')::UUID);

-- Families table uses its own id
CREATE POLICY tenant_isolation_families ON families
    USING (id = current_setting('app.current_family_id')::UUID);
