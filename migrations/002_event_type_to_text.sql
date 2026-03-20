-- Migration: Convert event_type from enum to free-form TEXT
-- This allows the LLM to return any descriptive event type without enum validation errors.

-- Step 1: Change the column type from enum to TEXT
ALTER TABLE events
    ALTER COLUMN type TYPE TEXT USING type::TEXT;

-- Step 2: Update the default to a plain text value (removes enum cast dependency)
ALTER TABLE events
    ALTER COLUMN type SET DEFAULT 'other';

-- Step 3: Drop the now-unused enum type
DROP TYPE IF EXISTS event_type;
