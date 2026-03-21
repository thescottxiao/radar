-- Migration: Add family knowledge & preferences tables/columns
-- Supports three-tier storage model: structured prefs, freeform learnings, staging area

-- Step 1: Add new columns to family_learnings
ALTER TABLE family_learnings
    ADD COLUMN IF NOT EXISTS caregiver_id UUID REFERENCES caregivers(id) ON DELETE SET NULL;

ALTER TABLE family_learnings
    ADD COLUMN IF NOT EXISTS graduated BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE family_learnings
    ADD COLUMN IF NOT EXISTS superseded_by UUID REFERENCES family_learnings(id);

-- Step 2: Update category CHECK constraint to include pref_* categories
-- Drop the old constraint and recreate with expanded values
ALTER TABLE family_learnings DROP CONSTRAINT IF EXISTS family_learnings_category_check;
ALTER TABLE family_learnings ADD CONSTRAINT family_learnings_category_check CHECK (category IN (
    'child_school', 'child_activity', 'child_friend', 'contact',
    'gear', 'schedule_pattern', 'budget',
    'pref_communication', 'pref_scheduling', 'pref_notification',
    'pref_prep', 'pref_delegation', 'pref_decision'
));

-- Step 3: Add indexes for new query patterns
CREATE INDEX IF NOT EXISTS idx_learnings_caregiver
    ON family_learnings(caregiver_id) WHERE caregiver_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_learnings_active
    ON family_learnings(family_id, confirmed)
    WHERE confirmed = TRUE AND graduated = FALSE AND superseded_by IS NULL;

-- Step 4: Create caregiver_preferences table
CREATE TABLE IF NOT EXISTS caregiver_preferences (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    caregiver_id    UUID NOT NULL UNIQUE REFERENCES caregivers(id) ON DELETE CASCADE,
    family_id       UUID NOT NULL REFERENCES families(id) ON DELETE CASCADE,
    quiet_hours_start TIME,
    quiet_hours_end   TIME,
    delegation_areas  TEXT[],
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_caregiver_prefs_family
    ON caregiver_preferences(family_id);

-- Step 5: RLS policy for caregiver_preferences
ALTER TABLE caregiver_preferences ENABLE ROW LEVEL SECURITY;
CREATE POLICY IF NOT EXISTS tenant_isolation ON caregiver_preferences
    USING (family_id = current_setting('app.current_family_id')::UUID);
