-- Seed a test family for local development
-- This lets you test the full WhatsApp flow without OAuth
-- Re-run safely — all inserts use ON CONFLICT DO NOTHING

-- ── Family ──────────────────────────────────────────────────────────
INSERT INTO families (id, whatsapp_group_id, forward_email, onboarding_complete, timezone)
VALUES (
    'a0000000-0000-0000-0000-000000000001',
    'test-group-001',
    'test-family@localhost',
    true,
    'America/New_York'
) ON CONFLICT (id) DO NOTHING;

-- ── Caregivers ──────────────────────────────────────────────────────
INSERT INTO caregivers (id, family_id, whatsapp_phone, name, is_active)
VALUES
    ('b0000000-0000-0000-0000-000000000001', 'a0000000-0000-0000-0000-000000000001',
     '+15551234567', 'Scott', true),
    ('b0000000-0000-0000-0000-000000000002', 'a0000000-0000-0000-0000-000000000001',
     '+15559876543', 'Lisa', true),
    ('b0000000-0000-0000-0000-000000000007', 'a0000000-0000-0000-0000-000000000001',
     '+16502859563', 'Nick Rizzo', true)
ON CONFLICT (id) DO NOTHING;

-- ── Children ────────────────────────────────────────────────────────
INSERT INTO children (id, family_id, name, school, grade, activities)
VALUES
    ('c0000000-0000-0000-0000-000000000001', 'a0000000-0000-0000-0000-000000000001',
     'Emma', 'Lincoln Elementary', '3rd', ARRAY['soccer', 'piano', 'girl scouts']),
    ('c0000000-0000-0000-0000-000000000002', 'a0000000-0000-0000-0000-000000000001',
     'Jake', 'Lincoln Elementary', '1st', ARRAY['swimming', 'art', 'T-ball'])
ON CONFLICT (id) DO NOTHING;

-- ── Helper: base dates ──────────────────────────────────────────────
-- today_4pm, tomorrow_4pm, etc. for readable event times
-- Using CURRENT_DATE so events are always in the future

-- ── Events ──────────────────────────────────────────────────────────

-- TODAY: Emma's piano lesson (5:00–5:45 PM)
INSERT INTO events (id, family_id, title, type, datetime_start, datetime_end, location, source, extraction_confidence)
VALUES (
    'd0000000-0000-0000-0000-000000000001',
    'a0000000-0000-0000-0000-000000000001',
    'Piano Lesson',
    'other',
    CURRENT_DATE + TIME '17:00',
    CURRENT_DATE + TIME '17:45',
    'Ms. Chen''s Studio, 42 Oak Ave',
    'manual', 1.0
) ON CONFLICT (id) DO NOTHING;

-- TOMORROW: Emma's soccer practice (4:00–5:30 PM)
INSERT INTO events (id, family_id, title, type, datetime_start, datetime_end, location, source, extraction_confidence)
VALUES (
    'd0000000-0000-0000-0000-000000000002',
    'a0000000-0000-0000-0000-000000000001',
    'Soccer Practice',
    'sports_practice',
    CURRENT_DATE + INTERVAL '1 day' + TIME '16:00',
    CURRENT_DATE + INTERVAL '1 day' + TIME '17:30',
    'Lincoln Park Field 3',
    'manual', 1.0
) ON CONFLICT (id) DO NOTHING;

-- TOMORROW: Jake's swim class (4:30–5:15 PM) — overlaps with Emma's soccer
INSERT INTO events (id, family_id, title, type, datetime_start, datetime_end, location, source, extraction_confidence)
VALUES (
    'd0000000-0000-0000-0000-000000000003',
    'a0000000-0000-0000-0000-000000000001',
    'Swim Class',
    'sports_practice',
    CURRENT_DATE + INTERVAL '1 day' + TIME '16:30',
    CURRENT_DATE + INTERVAL '1 day' + TIME '17:15',
    'YMCA Pool, 200 Main St',
    'manual', 1.0
) ON CONFLICT (id) DO NOTHING;

-- DAY AFTER TOMORROW: Jake's T-ball game (10:00 AM–12:00 PM)
INSERT INTO events (id, family_id, title, type, datetime_start, datetime_end, location, source, extraction_confidence)
VALUES (
    'd0000000-0000-0000-0000-000000000004',
    'a0000000-0000-0000-0000-000000000001',
    'T-Ball Game vs Blue Jays',
    'sports_game',
    CURRENT_DATE + INTERVAL '2 days' + TIME '10:00',
    CURRENT_DATE + INTERVAL '2 days' + TIME '12:00',
    'Riverside Diamond',
    'email', 0.9
) ON CONFLICT (id) DO NOTHING;

-- 3 DAYS: Emma's Girl Scout meeting (3:30–5:00 PM)
INSERT INTO events (id, family_id, title, type, datetime_start, datetime_end, location, source, extraction_confidence)
VALUES (
    'd0000000-0000-0000-0000-000000000005',
    'a0000000-0000-0000-0000-000000000001',
    'Girl Scout Troop 412 Meeting',
    'other',
    CURRENT_DATE + INTERVAL '3 days' + TIME '15:30',
    CURRENT_DATE + INTERVAL '3 days' + TIME '17:00',
    'Lincoln Elementary Cafeteria',
    'email', 0.85
) ON CONFLICT (id) DO NOTHING;

-- 4 DAYS: Both kids — school field trip (9:00 AM–2:00 PM)
INSERT INTO events (id, family_id, title, type, datetime_start, datetime_end, location, source, extraction_confidence)
VALUES (
    'd0000000-0000-0000-0000-000000000006',
    'a0000000-0000-0000-0000-000000000001',
    'School Field Trip to Science Museum',
    'school_event',
    CURRENT_DATE + INTERVAL '4 days' + TIME '09:00',
    CURRENT_DATE + INTERVAL '4 days' + TIME '14:00',
    'City Science Museum, 500 Discovery Blvd',
    'email', 0.95
) ON CONFLICT (id) DO NOTHING;

-- 5 DAYS: Emma's soccer game (Saturday morning, 9:00–10:30 AM)
INSERT INTO events (id, family_id, title, type, datetime_start, datetime_end, location, source, extraction_confidence)
VALUES (
    'd0000000-0000-0000-0000-000000000007',
    'a0000000-0000-0000-0000-000000000001',
    'Soccer Game vs Thunder',
    'sports_game',
    CURRENT_DATE + INTERVAL '5 days' + TIME '09:00',
    CURRENT_DATE + INTERVAL '5 days' + TIME '10:30',
    'Westside Sports Complex Field 2',
    'calendar', 1.0
) ON CONFLICT (id) DO NOTHING;

-- 5 DAYS: Jake's art class (10:00–11:30 AM)
INSERT INTO events (id, family_id, title, type, datetime_start, datetime_end, location, source, extraction_confidence)
VALUES (
    'd0000000-0000-0000-0000-000000000008',
    'a0000000-0000-0000-0000-000000000001',
    'Art Class — Clay Sculpting',
    'other',
    CURRENT_DATE + INTERVAL '5 days' + TIME '10:00',
    CURRENT_DATE + INTERVAL '5 days' + TIME '11:30',
    'Creative Kids Studio, 88 Elm St',
    'manual', 1.0
) ON CONFLICT (id) DO NOTHING;

-- 6 DAYS: Birthday party for Jake's friend (2:00–4:00 PM)
INSERT INTO events (id, family_id, title, type, datetime_start, datetime_end, location, source, extraction_confidence,
                    rsvp_status, rsvp_deadline, rsvp_method, rsvp_contact)
VALUES (
    'd0000000-0000-0000-0000-000000000009',
    'a0000000-0000-0000-0000-000000000001',
    'Liam''s Birthday Party',
    'birthday_party',
    CURRENT_DATE + INTERVAL '6 days' + TIME '14:00',
    CURRENT_DATE + INTERVAL '6 days' + TIME '16:00',
    'Jump Zone Trampoline Park',
    'email', 0.92,
    'pending',
    CURRENT_DATE + INTERVAL '4 days' + TIME '18:00',
    'reply_email',
    'liam.mom@gmail.com'
) ON CONFLICT (id) DO NOTHING;

-- 7 DAYS: Emma's dental appointment (3:00–3:45 PM)
INSERT INTO events (id, family_id, title, type, datetime_start, datetime_end, location, source, extraction_confidence)
VALUES (
    'd0000000-0000-0000-0000-000000000010',
    'a0000000-0000-0000-0000-000000000001',
    'Emma Dental Checkup',
    'dental_appointment',
    CURRENT_DATE + INTERVAL '7 days' + TIME '15:00',
    CURRENT_DATE + INTERVAL '7 days' + TIME '15:45',
    'Bright Smiles Pediatric Dentistry',
    'calendar', 1.0
) ON CONFLICT (id) DO NOTHING;

-- ── Event ↔ Child Links ─────────────────────────────────────────────

INSERT INTO event_children (event_id, child_id, family_id) VALUES
    -- Piano: Emma
    ('d0000000-0000-0000-0000-000000000001', 'c0000000-0000-0000-0000-000000000001', 'a0000000-0000-0000-0000-000000000001'),
    -- Soccer practice: Emma
    ('d0000000-0000-0000-0000-000000000002', 'c0000000-0000-0000-0000-000000000001', 'a0000000-0000-0000-0000-000000000001'),
    -- Swim: Jake
    ('d0000000-0000-0000-0000-000000000003', 'c0000000-0000-0000-0000-000000000002', 'a0000000-0000-0000-0000-000000000001'),
    -- T-ball: Jake
    ('d0000000-0000-0000-0000-000000000004', 'c0000000-0000-0000-0000-000000000002', 'a0000000-0000-0000-0000-000000000001'),
    -- Girl Scouts: Emma
    ('d0000000-0000-0000-0000-000000000005', 'c0000000-0000-0000-0000-000000000001', 'a0000000-0000-0000-0000-000000000001'),
    -- Field trip: both kids
    ('d0000000-0000-0000-0000-000000000006', 'c0000000-0000-0000-0000-000000000001', 'a0000000-0000-0000-0000-000000000001'),
    ('d0000000-0000-0000-0000-000000000006', 'c0000000-0000-0000-0000-000000000002', 'a0000000-0000-0000-0000-000000000001'),
    -- Soccer game: Emma
    ('d0000000-0000-0000-0000-000000000007', 'c0000000-0000-0000-0000-000000000001', 'a0000000-0000-0000-0000-000000000001'),
    -- Art class: Jake
    ('d0000000-0000-0000-0000-000000000008', 'c0000000-0000-0000-0000-000000000002', 'a0000000-0000-0000-0000-000000000001'),
    -- Birthday party: Jake
    ('d0000000-0000-0000-0000-000000000009', 'c0000000-0000-0000-0000-000000000002', 'a0000000-0000-0000-0000-000000000001'),
    -- Dental: Emma
    ('d0000000-0000-0000-0000-000000000010', 'c0000000-0000-0000-0000-000000000001', 'a0000000-0000-0000-0000-000000000001')
ON CONFLICT DO NOTHING;

-- ── Action Items ────────────────────────────────────────────────────

-- Permission slip for field trip (due in 3 days)
INSERT INTO action_items (id, family_id, event_id, description, due_date, source, source_ref, status)
VALUES (
    'e0000000-0000-0000-0000-000000000001',
    'a0000000-0000-0000-0000-000000000001',
    'd0000000-0000-0000-0000-000000000006',
    'Return signed permission slip for Science Museum field trip',
    CURRENT_DATE + INTERVAL '3 days',
    'email', 'school-newsletter-032026',
    'pending'
) ON CONFLICT (id) DO NOTHING;

-- RSVP to Liam's birthday (due in 4 days)
INSERT INTO action_items (id, family_id, event_id, description, due_date, source, source_ref, status)
VALUES (
    'e0000000-0000-0000-0000-000000000002',
    'a0000000-0000-0000-0000-000000000001',
    'd0000000-0000-0000-0000-000000000009',
    'RSVP to Liam''s birthday party — reply to liam.mom@gmail.com',
    CURRENT_DATE + INTERVAL '4 days',
    'email', 'birthday-invite-liam',
    'pending'
) ON CONFLICT (id) DO NOTHING;

-- Bring snack for soccer game (due in 5 days)
INSERT INTO action_items (id, family_id, event_id, description, due_date, source, source_ref, status)
VALUES (
    'e0000000-0000-0000-0000-000000000003',
    'a0000000-0000-0000-0000-000000000001',
    'd0000000-0000-0000-0000-000000000007',
    'It''s our turn to bring team snacks for the soccer game — 15 kids',
    CURRENT_DATE + INTERVAL '5 days',
    'email', 'soccer-team-email',
    'pending'
) ON CONFLICT (id) DO NOTHING;

-- Summer camp registration deadline (due in 10 days)
INSERT INTO action_items (id, family_id, description, due_date, source, source_ref, status, type)
VALUES (
    'e0000000-0000-0000-0000-000000000004',
    'a0000000-0000-0000-0000-000000000001',
    'Register Emma for summer soccer camp — early bird discount ends soon',
    CURRENT_DATE + INTERVAL '10 days',
    'email', 'soccer-camp-flyer',
    'pending',
    'registration_deadline'
) ON CONFLICT (id) DO NOTHING;

-- ── Family Learnings ────────────────────────────────────────────────

INSERT INTO family_learnings (id, family_id, category, fact, confirmed)
VALUES
    (uuid_generate_v4(), 'a0000000-0000-0000-0000-000000000001',
     'preference', 'Jake is allergic to peanuts', true),
    (uuid_generate_v4(), 'a0000000-0000-0000-0000-000000000001',
     'preference', 'Emma prefers to be picked up, not take the bus', true),
    (uuid_generate_v4(), 'a0000000-0000-0000-0000-000000000001',
     'gear', 'Soccer gear is in the garage by the blue bin', false)
ON CONFLICT DO NOTHING;


-- ════════════════════════════════════════════════════════════════════
-- FAMILY 2: The Parkers
-- ════════════════════════════════════════════════════════════════════

INSERT INTO families (id, whatsapp_group_id, forward_email, onboarding_complete, timezone)
VALUES (
    'a0000000-0000-0000-0000-000000000002',
    'test-group-002',
    'parker-family@localhost',
    true,
    'America/New_York'
) ON CONFLICT (id) DO NOTHING;

-- ── Caregivers ──────────────────────────────────────────────────────
INSERT INTO caregivers (id, family_id, whatsapp_phone, name, is_active)
VALUES
    ('b0000000-0000-0000-0000-000000000003', 'a0000000-0000-0000-0000-000000000002',
     '+12015270025', 'Alex', true),
    ('b0000000-0000-0000-0000-000000000004', 'a0000000-0000-0000-0000-000000000002',
     '+15553334444', 'Jordan', true)
ON CONFLICT (id) DO NOTHING;

-- ── Children ────────────────────────────────────────────────────────
INSERT INTO children (id, family_id, name, school, grade, activities)
VALUES
    ('c0000000-0000-0000-0000-000000000003', 'a0000000-0000-0000-0000-000000000002',
     'Mia', 'Oakwood Academy', '5th', ARRAY['basketball', 'violin', 'coding club']),
    ('c0000000-0000-0000-0000-000000000004', 'a0000000-0000-0000-0000-000000000002',
     'Noah', 'Oakwood Academy', '2nd', ARRAY['karate', 'soccer', 'legos'])
ON CONFLICT (id) DO NOTHING;

-- ── Events ──────────────────────────────────────────────────────────

-- TODAY: Mia's violin lesson (4:00–4:45 PM)
INSERT INTO events (id, family_id, title, type, datetime_start, datetime_end, location, source, extraction_confidence)
VALUES (
    'd0000000-0000-0000-0000-000000000011',
    'a0000000-0000-0000-0000-000000000002',
    'Violin Lesson',
    'other',
    CURRENT_DATE + TIME '16:00',
    CURRENT_DATE + TIME '16:45',
    'Harmony Music School, 15 Maple Dr',
    'manual', 1.0
) ON CONFLICT (id) DO NOTHING;

-- TOMORROW: Noah's karate class (5:00–6:00 PM)
INSERT INTO events (id, family_id, title, type, datetime_start, datetime_end, location, source, extraction_confidence)
VALUES (
    'd0000000-0000-0000-0000-000000000012',
    'a0000000-0000-0000-0000-000000000002',
    'Karate Class',
    'sports_practice',
    CURRENT_DATE + INTERVAL '1 day' + TIME '17:00',
    CURRENT_DATE + INTERVAL '1 day' + TIME '18:00',
    'Tiger Martial Arts, 300 Pine St',
    'manual', 1.0
) ON CONFLICT (id) DO NOTHING;

-- TOMORROW: Mia's basketball practice (5:30–7:00 PM) — overlaps with Noah's karate
INSERT INTO events (id, family_id, title, type, datetime_start, datetime_end, location, source, extraction_confidence)
VALUES (
    'd0000000-0000-0000-0000-000000000013',
    'a0000000-0000-0000-0000-000000000002',
    'Basketball Practice',
    'sports_practice',
    CURRENT_DATE + INTERVAL '1 day' + TIME '17:30',
    CURRENT_DATE + INTERVAL '1 day' + TIME '19:00',
    'Oakwood Academy Gym',
    'manual', 1.0
) ON CONFLICT (id) DO NOTHING;

-- 2 DAYS: Noah's soccer game (9:00–10:30 AM)
INSERT INTO events (id, family_id, title, type, datetime_start, datetime_end, location, source, extraction_confidence)
VALUES (
    'd0000000-0000-0000-0000-000000000014',
    'a0000000-0000-0000-0000-000000000002',
    'Soccer Game vs Wildcats',
    'sports_game',
    CURRENT_DATE + INTERVAL '2 days' + TIME '09:00',
    CURRENT_DATE + INTERVAL '2 days' + TIME '10:30',
    'Central Park Field 1',
    'email', 0.9
) ON CONFLICT (id) DO NOTHING;

-- 3 DAYS: Mia's coding club (3:30–5:00 PM)
INSERT INTO events (id, family_id, title, type, datetime_start, datetime_end, location, source, extraction_confidence)
VALUES (
    'd0000000-0000-0000-0000-000000000015',
    'a0000000-0000-0000-0000-000000000002',
    'Coding Club — Scratch Projects',
    'school_event',
    CURRENT_DATE + INTERVAL '3 days' + TIME '15:30',
    CURRENT_DATE + INTERVAL '3 days' + TIME '17:00',
    'Oakwood Academy Computer Lab',
    'email', 0.85
) ON CONFLICT (id) DO NOTHING;

-- 4 DAYS: Noah's friend's birthday party (1:00–3:00 PM)
INSERT INTO events (id, family_id, title, type, datetime_start, datetime_end, location, source, extraction_confidence,
                    rsvp_status, rsvp_deadline, rsvp_method, rsvp_contact)
VALUES (
    'd0000000-0000-0000-0000-000000000016',
    'a0000000-0000-0000-0000-000000000002',
    'Ethan''s Birthday Party',
    'birthday_party',
    CURRENT_DATE + INTERVAL '4 days' + TIME '13:00',
    CURRENT_DATE + INTERVAL '4 days' + TIME '15:00',
    'Laser Tag Arena, 77 Fun Blvd',
    'email', 0.88,
    'pending',
    CURRENT_DATE + INTERVAL '2 days' + TIME '20:00',
    'reply_email',
    'ethan.dad@gmail.com'
) ON CONFLICT (id) DO NOTHING;

-- 5 DAYS: Mia's basketball game (11:00 AM–12:30 PM)
INSERT INTO events (id, family_id, title, type, datetime_start, datetime_end, location, source, extraction_confidence)
VALUES (
    'd0000000-0000-0000-0000-000000000017',
    'a0000000-0000-0000-0000-000000000002',
    'Basketball Game vs Hawks',
    'sports_game',
    CURRENT_DATE + INTERVAL '5 days' + TIME '11:00',
    CURRENT_DATE + INTERVAL '5 days' + TIME '12:30',
    'Riverside Recreation Center',
    'calendar', 1.0
) ON CONFLICT (id) DO NOTHING;

-- 6 DAYS: Both kids — school spring concert (6:00–8:00 PM)
INSERT INTO events (id, family_id, title, type, datetime_start, datetime_end, location, source, extraction_confidence)
VALUES (
    'd0000000-0000-0000-0000-000000000018',
    'a0000000-0000-0000-0000-000000000002',
    'Spring Concert — Oakwood Academy',
    'school_event',
    CURRENT_DATE + INTERVAL '6 days' + TIME '18:00',
    CURRENT_DATE + INTERVAL '6 days' + TIME '20:00',
    'Oakwood Academy Auditorium',
    'email', 0.95
) ON CONFLICT (id) DO NOTHING;

-- ── Event ↔ Child Links ─────────────────────────────────────────────

INSERT INTO event_children (event_id, child_id, family_id) VALUES
    -- Violin: Mia
    ('d0000000-0000-0000-0000-000000000011', 'c0000000-0000-0000-0000-000000000003', 'a0000000-0000-0000-0000-000000000002'),
    -- Karate: Noah
    ('d0000000-0000-0000-0000-000000000012', 'c0000000-0000-0000-0000-000000000004', 'a0000000-0000-0000-0000-000000000002'),
    -- Basketball practice: Mia
    ('d0000000-0000-0000-0000-000000000013', 'c0000000-0000-0000-0000-000000000003', 'a0000000-0000-0000-0000-000000000002'),
    -- Soccer game: Noah
    ('d0000000-0000-0000-0000-000000000014', 'c0000000-0000-0000-0000-000000000004', 'a0000000-0000-0000-0000-000000000002'),
    -- Coding club: Mia
    ('d0000000-0000-0000-0000-000000000015', 'c0000000-0000-0000-0000-000000000003', 'a0000000-0000-0000-0000-000000000002'),
    -- Birthday party: Noah
    ('d0000000-0000-0000-0000-000000000016', 'c0000000-0000-0000-0000-000000000004', 'a0000000-0000-0000-0000-000000000002'),
    -- Basketball game: Mia
    ('d0000000-0000-0000-0000-000000000017', 'c0000000-0000-0000-0000-000000000003', 'a0000000-0000-0000-0000-000000000002'),
    -- Spring concert: both kids
    ('d0000000-0000-0000-0000-000000000018', 'c0000000-0000-0000-0000-000000000003', 'a0000000-0000-0000-0000-000000000002'),
    ('d0000000-0000-0000-0000-000000000018', 'c0000000-0000-0000-0000-000000000004', 'a0000000-0000-0000-0000-000000000002')
ON CONFLICT DO NOTHING;

-- ── Action Items ────────────────────────────────────────────────────

-- RSVP to Ethan's birthday (due in 2 days)
INSERT INTO action_items (id, family_id, event_id, description, due_date, source, source_ref, status)
VALUES (
    'e0000000-0000-0000-0000-000000000005',
    'a0000000-0000-0000-0000-000000000002',
    'd0000000-0000-0000-0000-000000000016',
    'RSVP to Ethan''s birthday party — reply to ethan.dad@gmail.com',
    CURRENT_DATE + INTERVAL '2 days',
    'email', 'birthday-invite-ethan',
    'pending'
) ON CONFLICT (id) DO NOTHING;

-- Buy new basketball shoes for Mia (due in 5 days)
INSERT INTO action_items (id, family_id, description, due_date, source, source_ref, status, type)
VALUES (
    'e0000000-0000-0000-0000-000000000006',
    'a0000000-0000-0000-0000-000000000002',
    'Buy new basketball shoes for Mia — she outgrew her current pair',
    CURRENT_DATE + INTERVAL '5 days',
    'manual', NULL,
    'pending',
    'item_to_purchase'
) ON CONFLICT (id) DO NOTHING;

-- Spring concert volunteer sign-up (due in 4 days)
INSERT INTO action_items (id, family_id, event_id, description, due_date, source, source_ref, status)
VALUES (
    'e0000000-0000-0000-0000-000000000007',
    'a0000000-0000-0000-0000-000000000002',
    'd0000000-0000-0000-0000-000000000018',
    'Sign up for spring concert volunteer shift — setup or concessions',
    CURRENT_DATE + INTERVAL '4 days',
    'email', 'spring-concert-email',
    'pending'
) ON CONFLICT (id) DO NOTHING;

-- ── Family Learnings ────────────────────────────────────────────────

INSERT INTO family_learnings (id, family_id, category, fact, confirmed)
VALUES
    (uuid_generate_v4(), 'a0000000-0000-0000-0000-000000000002',
     'preference', 'Noah gets carsick on long drives — sit him in front', true),
    (uuid_generate_v4(), 'a0000000-0000-0000-0000-000000000002',
     'child_activity', 'Mia is first chair violin and performs in the spring concert', true),
    (uuid_generate_v4(), 'a0000000-0000-0000-0000-000000000002',
     'contact', 'Noah''s karate instructor is Sensei Mike — call 555-0199 for schedule changes', false)
ON CONFLICT DO NOTHING;


-- ════════════════════════════════════════════════════════════════════
-- FAMILY 3: The Nguyens
-- ════════════════════════════════════════════════════════════════════

INSERT INTO families (id, whatsapp_group_id, forward_email, onboarding_complete, timezone)
VALUES (
    'a0000000-0000-0000-0000-000000000003',
    'test-group-003',
    'nguyen-family@localhost',
    true,
    'America/Toronto'
) ON CONFLICT (id) DO NOTHING;

-- ── Caregivers ──────────────────────────────────────────────────────
INSERT INTO caregivers (id, family_id, whatsapp_phone, name, is_active)
VALUES
    ('b0000000-0000-0000-0000-000000000005', 'a0000000-0000-0000-0000-000000000003',
     '+14379896133', 'Sam', true),
    ('b0000000-0000-0000-0000-000000000006', 'a0000000-0000-0000-0000-000000000003',
     '+15556667777', 'Taylor', true)
ON CONFLICT (id) DO NOTHING;

-- ── Children ────────────────────────────────────────────────────────
INSERT INTO children (id, family_id, name, school, grade, activities)
VALUES
    ('c0000000-0000-0000-0000-000000000005', 'a0000000-0000-0000-0000-000000000003',
     'Lily', 'Greenfield Middle School', '6th', ARRAY['dance', 'gymnastics', 'math team']),
    ('c0000000-0000-0000-0000-000000000006', 'a0000000-0000-0000-0000-000000000003',
     'Owen', 'Greenfield Elementary', 'K', ARRAY['swimming', 'storytime', 'mini soccer'])
ON CONFLICT (id) DO NOTHING;

-- ── Events ──────────────────────────────────────────────────────────

-- TODAY: Lily's dance rehearsal (4:30–6:00 PM)
INSERT INTO events (id, family_id, title, type, datetime_start, datetime_end, location, source, extraction_confidence)
VALUES (
    'd0000000-0000-0000-0000-000000000021',
    'a0000000-0000-0000-0000-000000000003',
    'Dance Rehearsal — Spring Recital Prep',
    'sports_practice',
    CURRENT_DATE + TIME '16:30',
    CURRENT_DATE + TIME '18:00',
    'Studio B, DanceFusion Academy',
    'manual', 1.0
) ON CONFLICT (id) DO NOTHING;

-- TOMORROW: Owen's swim lesson (10:00–10:45 AM)
INSERT INTO events (id, family_id, title, type, datetime_start, datetime_end, location, source, extraction_confidence)
VALUES (
    'd0000000-0000-0000-0000-000000000022',
    'a0000000-0000-0000-0000-000000000003',
    'Swim Lesson — Level 2',
    'sports_practice',
    CURRENT_DATE + INTERVAL '1 day' + TIME '10:00',
    CURRENT_DATE + INTERVAL '1 day' + TIME '10:45',
    'Greenfield Community Pool',
    'manual', 1.0
) ON CONFLICT (id) DO NOTHING;

-- TOMORROW: Lily's gymnastics (4:00–5:30 PM)
INSERT INTO events (id, family_id, title, type, datetime_start, datetime_end, location, source, extraction_confidence)
VALUES (
    'd0000000-0000-0000-0000-000000000023',
    'a0000000-0000-0000-0000-000000000003',
    'Gymnastics Practice',
    'sports_practice',
    CURRENT_DATE + INTERVAL '1 day' + TIME '16:00',
    CURRENT_DATE + INTERVAL '1 day' + TIME '17:30',
    'FlipStar Gymnastics, 120 River Rd',
    'manual', 1.0
) ON CONFLICT (id) DO NOTHING;

-- 2 DAYS: Owen's storytime at the library (10:30–11:15 AM)
INSERT INTO events (id, family_id, title, type, datetime_start, datetime_end, location, source, extraction_confidence)
VALUES (
    'd0000000-0000-0000-0000-000000000024',
    'a0000000-0000-0000-0000-000000000003',
    'Library Storytime',
    'other',
    CURRENT_DATE + INTERVAL '2 days' + TIME '10:30',
    CURRENT_DATE + INTERVAL '2 days' + TIME '11:15',
    'Greenfield Public Library, Kids Wing',
    'manual', 1.0
) ON CONFLICT (id) DO NOTHING;

-- 3 DAYS: Lily's math team competition (8:30 AM–12:00 PM)
INSERT INTO events (id, family_id, title, type, datetime_start, datetime_end, location, source, extraction_confidence)
VALUES (
    'd0000000-0000-0000-0000-000000000025',
    'a0000000-0000-0000-0000-000000000003',
    'Regional Math Team Competition',
    'school_event',
    CURRENT_DATE + INTERVAL '3 days' + TIME '08:30',
    CURRENT_DATE + INTERVAL '3 days' + TIME '12:00',
    'Westbrook High School Auditorium',
    'email', 0.9
) ON CONFLICT (id) DO NOTHING;

-- 4 DAYS: Owen's mini soccer (9:00–10:00 AM)
INSERT INTO events (id, family_id, title, type, datetime_start, datetime_end, location, source, extraction_confidence)
VALUES (
    'd0000000-0000-0000-0000-000000000026',
    'a0000000-0000-0000-0000-000000000003',
    'Mini Soccer — Kicking Stars',
    'sports_practice',
    CURRENT_DATE + INTERVAL '4 days' + TIME '09:00',
    CURRENT_DATE + INTERVAL '4 days' + TIME '10:00',
    'Greenfield Rec Center Turf',
    'manual', 1.0
) ON CONFLICT (id) DO NOTHING;

-- 5 DAYS: Lily's dance recital (7:00–9:00 PM)
INSERT INTO events (id, family_id, title, type, datetime_start, datetime_end, location, source, extraction_confidence)
VALUES (
    'd0000000-0000-0000-0000-000000000027',
    'a0000000-0000-0000-0000-000000000003',
    'Spring Dance Recital',
    'recital_performance',
    CURRENT_DATE + INTERVAL '5 days' + TIME '19:00',
    CURRENT_DATE + INTERVAL '5 days' + TIME '21:00',
    'Greenfield Performing Arts Center',
    'email', 0.95
) ON CONFLICT (id) DO NOTHING;

-- 6 DAYS: Both kids — family dentist appointments (back to back)
INSERT INTO events (id, family_id, title, type, datetime_start, datetime_end, location, source, extraction_confidence)
VALUES (
    'd0000000-0000-0000-0000-000000000028',
    'a0000000-0000-0000-0000-000000000003',
    'Lily Dental Cleaning',
    'dental_appointment',
    CURRENT_DATE + INTERVAL '6 days' + TIME '09:00',
    CURRENT_DATE + INTERVAL '6 days' + TIME '09:45',
    'Smile Kids Dental, 50 Birch Lane',
    'calendar', 1.0
) ON CONFLICT (id) DO NOTHING;

INSERT INTO events (id, family_id, title, type, datetime_start, datetime_end, location, source, extraction_confidence)
VALUES (
    'd0000000-0000-0000-0000-000000000029',
    'a0000000-0000-0000-0000-000000000003',
    'Owen Dental Cleaning',
    'dental_appointment',
    CURRENT_DATE + INTERVAL '6 days' + TIME '10:00',
    CURRENT_DATE + INTERVAL '6 days' + TIME '10:45',
    'Smile Kids Dental, 50 Birch Lane',
    'calendar', 1.0
) ON CONFLICT (id) DO NOTHING;

-- ── Event ↔ Child Links ─────────────────────────────────────────────

INSERT INTO event_children (event_id, child_id, family_id) VALUES
    -- Dance rehearsal: Lily
    ('d0000000-0000-0000-0000-000000000021', 'c0000000-0000-0000-0000-000000000005', 'a0000000-0000-0000-0000-000000000003'),
    -- Swim lesson: Owen
    ('d0000000-0000-0000-0000-000000000022', 'c0000000-0000-0000-0000-000000000006', 'a0000000-0000-0000-0000-000000000003'),
    -- Gymnastics: Lily
    ('d0000000-0000-0000-0000-000000000023', 'c0000000-0000-0000-0000-000000000005', 'a0000000-0000-0000-0000-000000000003'),
    -- Storytime: Owen
    ('d0000000-0000-0000-0000-000000000024', 'c0000000-0000-0000-0000-000000000006', 'a0000000-0000-0000-0000-000000000003'),
    -- Math competition: Lily
    ('d0000000-0000-0000-0000-000000000025', 'c0000000-0000-0000-0000-000000000005', 'a0000000-0000-0000-0000-000000000003'),
    -- Mini soccer: Owen
    ('d0000000-0000-0000-0000-000000000026', 'c0000000-0000-0000-0000-000000000006', 'a0000000-0000-0000-0000-000000000003'),
    -- Dance recital: Lily
    ('d0000000-0000-0000-0000-000000000027', 'c0000000-0000-0000-0000-000000000005', 'a0000000-0000-0000-0000-000000000003'),
    -- Dental: Lily
    ('d0000000-0000-0000-0000-000000000028', 'c0000000-0000-0000-0000-000000000005', 'a0000000-0000-0000-0000-000000000003'),
    -- Dental: Owen
    ('d0000000-0000-0000-0000-000000000029', 'c0000000-0000-0000-0000-000000000006', 'a0000000-0000-0000-0000-000000000003')
ON CONFLICT DO NOTHING;

-- ── Action Items ────────────────────────────────────────────────────

-- Buy recital costume for Lily (due in 3 days)
INSERT INTO action_items (id, family_id, description, due_date, source, source_ref, status, type)
VALUES (
    'e0000000-0000-0000-0000-000000000008',
    'a0000000-0000-0000-0000-000000000003',
    'Order Lily''s recital costume from DanceFusion — black leotard + silver skirt (size 10)',
    CURRENT_DATE + INTERVAL '3 days',
    'email', 'dance-recital-email',
    'pending',
    'item_to_purchase'
) ON CONFLICT (id) DO NOTHING;

-- Pack math team supplies (due in 3 days)
INSERT INTO action_items (id, family_id, event_id, description, due_date, source, source_ref, status)
VALUES (
    'e0000000-0000-0000-0000-000000000009',
    'a0000000-0000-0000-0000-000000000003',
    'd0000000-0000-0000-0000-000000000025',
    'Pack calculator, #2 pencils, and water bottle for math competition',
    CURRENT_DATE + INTERVAL '3 days',
    'email', 'math-team-email',
    'pending'
) ON CONFLICT (id) DO NOTHING;

-- Register Owen for summer swim camp (due in 8 days)
INSERT INTO action_items (id, family_id, description, due_date, source, source_ref, status, type)
VALUES (
    'e0000000-0000-0000-0000-000000000010',
    'a0000000-0000-0000-0000-000000000003',
    'Register Owen for summer swim camp at Greenfield Pool — spots filling up',
    CURRENT_DATE + INTERVAL '8 days',
    'email', 'swim-camp-flyer',
    'pending',
    'registration_deadline'
) ON CONFLICT (id) DO NOTHING;

-- ── Family Learnings ────────────────────────────────────────────────

INSERT INTO family_learnings (id, family_id, category, fact, confirmed)
VALUES
    (uuid_generate_v4(), 'a0000000-0000-0000-0000-000000000003',
     'preference', 'Owen needs his blue goggles for swim — refuses to wear any other pair', true),
    (uuid_generate_v4(), 'a0000000-0000-0000-0000-000000000003',
     'child_activity', 'Lily is competing in the regional math team finals this year', true),
    (uuid_generate_v4(), 'a0000000-0000-0000-0000-000000000003',
     'contact', 'Lily''s dance teacher is Ms. Rivera — text 555-0342 for rehearsal changes', false)
ON CONFLICT DO NOTHING;
