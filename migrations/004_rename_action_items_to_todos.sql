-- Migration: Rename action_items to todos
-- Introduces first-class Todo concept with GCal sync and smart reminders.

BEGIN;

-- 1. Rename enum types
ALTER TYPE action_item_type RENAME TO todo_type;
ALTER TYPE action_item_status RENAME TO todo_status;

-- 2. Rename main table
ALTER TABLE action_items RENAME TO todos;

-- 3. Add new columns for GCal sync and reminders
ALTER TABLE todos ADD COLUMN gcal_event_id TEXT;
ALTER TABLE todos ADD COLUMN reminder_days_before INTEGER;
ALTER TABLE todos ADD COLUMN reminder_sent_at TIMESTAMPTZ;
ALTER TABLE todos ADD COLUMN confirmed_by_caregiver BOOLEAN NOT NULL DEFAULT FALSE;

-- 4. Rename junction table and its FK column
ALTER TABLE action_item_children RENAME TO todo_children;
ALTER TABLE todo_children RENAME COLUMN action_item_id TO todo_id;

-- 5. Rename indexes
ALTER INDEX idx_action_items_family RENAME TO idx_todos_family;
ALTER INDEX idx_action_items_status RENAME TO idx_todos_status;
ALTER INDEX idx_action_items_due RENAME TO idx_todos_due;

-- 6. Add new index for event-linked todos
CREATE INDEX idx_todos_event ON todos(event_id) WHERE event_id IS NOT NULL;

-- 7. Drop and recreate RLS policies with new names
DROP POLICY IF EXISTS tenant_isolation_action_items ON todos;
CREATE POLICY tenant_isolation_todos ON todos
    USING (family_id = current_setting('app.current_family_id')::UUID);

DROP POLICY IF EXISTS tenant_isolation_action_item_children ON todo_children;
CREATE POLICY tenant_isolation_todo_children ON todo_children
    USING (family_id = current_setting('app.current_family_id')::UUID);

-- 8. Add todo_id column to gcal_outbox for todo GCal entries
ALTER TABLE gcal_outbox ADD COLUMN todo_id UUID REFERENCES todos(id) ON DELETE SET NULL;

-- 9. Add todo_confirmation to pending_action_type enum
ALTER TYPE pending_action_type ADD VALUE IF NOT EXISTS 'todo_confirmation';

COMMIT;
