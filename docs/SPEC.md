# Radar — Product Specification

> A WhatsApp-native AI assistant that helps busy parents coordinate their kids' activities.

**Version:** 0.3.0
**Last updated:** 2026-03-19
**Status:** Phase 1 + Phase 2 implemented, live testing

---

## Table of Contents

1. [Product Overview](#1-product-overview)
2. [Target Customer](#2-target-customer)
3. [Family Model](#3-family-model)
4. [Data Sources and Ingestion](#4-data-sources-and-ingestion)
5. [Data Models](#5-data-models)
6. [Agent Specifications](#6-agent-specifications)
7. [Interaction Patterns](#7-interaction-patterns)
8. [Notification System](#8-notification-system)
9. [Onboarding](#9-onboarding)
10. [Recurring Events and Seasons](#10-recurring-events-and-seasons)
11. [Family Profile Learning](#11-family-profile-learning)
12. [Proactive Gap Detection](#12-proactive-gap-detection)
13. [Autonomy Matrix](#13-autonomy-matrix)
14. [Multi-Tenancy](#14-multi-tenancy)
15. [Security and Privacy](#15-security-and-privacy)
16. [Technical Stack](#16-technical-stack)
17. [Build Phases](#17-build-phases)
18. [Decision Log](#18-decision-log)
19. [Deferred Features](#19-deferred-features)

---

## 1. Product Overview

Radar is a multi-tenant SaaS product that lives in a WhatsApp group chat with a family's caregivers. It connects to each caregiver's Gmail and Google Calendar, automatically detects events, deadlines, and action items, and helps the family stay coordinated.

The product operates on a dual autonomy model:
- **AUTO:** Internal actions (calendar writes, reminders, family state updates) execute without approval.
- **SUGGEST:** External-facing actions (emails to other parents, RSVPs, purchase links, registrations) are drafted and presented for caregiver approval before execution.

### Core Capabilities

- Detect events and action items from incoming emails (school newsletters, party invites, league announcements, camp registrations)
- Sync calendars across all caregivers and flag scheduling conflicts
- Generate preparation checklists for events (gifts, gear, forms, RSVPs)
- Coordinate transportation logistics between caregivers
- Manage recurring activity schedules (sports seasons, music lessons, tutoring, etc.) with exception handling
- Send smart reminders (daily when actionable, weekly summary always)
- Accept forwarded emails and calendar URLs as an alternative to OAuth
- Accept voice notes via WhatsApp (transcribed and processed as text)
- Proactively identify gaps (missed appointments, scheduling opportunities, unscheduled activities)

---

## 2. Target Customer

### Primary Persona

Dual-income households with children ages 5–14 who are actively involved in activities (sports, school events, social events, camps). This is the window where coordination burden peaks — kids are old enough to have independent schedules but not old enough to manage them.

### Key Insights from User Research (7 interviews)

- Pain increases significantly around age 8 when kids start participating in multiple activities.
- More kids and more activities compound the problem non-linearly.
- One caregiver (often the mother) disproportionately shoulders the coordination burden.
- Every family interviewed uses Google Calendar + texting + in-person weekly meetings.
- The sub-tasks around events (buying gifts, signing waivers, packing gear) cause more stress than the events themselves.
- Playdate coordination is consistently cited as the most unstructured and difficult category.
- Important information is frequently buried in school emails that don't look like invitations.
- Work calendar conflicts are a major source of scheduling failures, but caregivers don't want to fully share work calendars with a family tool.
- Willingness to pay: $10–15/month at entry; up to $50/month for families with 3+ active kids who see full value.

### Trust Barriers

- Some users are not comfortable granting email OAuth access (Chantel: "Not open to giving an application access to my email, but if I can forward it to something and it includes it in the calendar").
- The forward-to-email and forward-to-calendar-URL features address this segment.

---

## 3. Family Model

### Core Principle

A family (tenant) is defined by a **WhatsApp group**. Any number of caregivers can be in the group. The bot participates as a group member. There are no defined roles — all caregivers have equal authority.

> **Current limitation:** WhatsApp Business API bots cannot be added to group chats. The bot currently communicates 1:1 with each caregiver. Group-chat support is a future consideration (see Decision Log). All notification and confirmation flows work in 1:1 mode — messages are sent to all caregivers in the family individually.

### Rules

- **Identification:** Each caregiver is identified by their WhatsApp phone number within the group.
- **Authority:** All caregivers can issue instructions, approve actions, and claim tasks with equal authority.
- **Concurrent input on AUTO actions:** For low-stakes internal actions (calendar writes, acknowledging info), the first response is sufficient. These are reversible and don't require group consensus.
- **Concurrent input on SUGGEST actions:** For external-facing actions (RSVPs, emails, purchases), if multiple caregivers respond within a short window and their responses contradict (e.g., one says "send it" while another says "wait" or edits the draft), the bot pauses execution and surfaces the conflict to the group. Two identical approvals (both say "yes") execute normally. The bot does not act on external actions until the conflict is resolved.
- **Sequential conflict resolution:** If a caregiver gives an instruction that contradicts an earlier instruction from another caregiver (e.g., one says "RSVP yes" and later another says "actually RSVP no"), the bot surfaces the conflict to the group and does not act until resolved.
- **Adding caregivers:** New members added to the WhatsApp group are automatically detected. The bot sends them a welcome message and OAuth link.
- **Removing caregivers:** Members who leave the group have their OAuth tokens revoked. Their historical data remains in the family profile.

---

## 4. Data Sources and Ingestion

### 4.1 Gmail Push Notifications (MVP)

- **Mechanism:** Google Pub/Sub push notifications via Gmail API `watch()`.
- **Trigger:** New email arrives in any connected caregiver's inbox.
- **Payload:** Raw email headers, body, and attachment metadata.
- **Renewal:** Watch channels expire every 7 days. Auto-renewal job runs on 5-day intervals (2-day buffer). Failed renewals trigger caregiver notification and fallback to polling.

### 4.2 Google Calendar Webhooks (MVP)

- **Mechanism:** GCal push notification channels via Calendar API.
- **Trigger:** Any event create, update, or delete on connected calendars.
- **Renewal:** Same 7-day expiry / 5-day renewal as Gmail.

### 4.3 WhatsApp Business API (MVP)

- **Provider:** Twilio or Meta Cloud API directly.
- **Inbound:** Webhook receives all messages from the family group (text and voice).
- **Outbound — Reactive:** Free-form messages within 24-hour conversation windows.
- **Outbound — Proactive:** Requires pre-approved WhatsApp message templates.
- **Voice notes:** Received as audio files, transcribed via Whisper API before processing.

### 4.4 Forward-to Email Address (MVP)

- **Mechanism:** Each family gets a unique inbound email address (e.g., `family-{id}@radar.app`).
- **Use case:** Caregivers who don't want to grant email OAuth can forward relevant emails manually. Also useful for forwarding from accounts the bot can't directly access.
- **Processing:** Forwarded emails enter the same extraction pipeline as Gmail push notifications.

### 4.5 ICS Feed Subscription (MVP)

- **Mechanism:** Family provides a URL to an .ics calendar feed (sports leagues, school calendars, activity providers).
- **Polling:** System fetches the ICS feed periodically (every 30 minutes) and diffs against stored events.
- **ICS file attachments:** Caregivers can also forward .ics file attachments to the bot's email address or upload them directly in WhatsApp.
  - **WhatsApp:** Document uploads with `.ics` extension or `text/calendar` MIME type are detected, downloaded from Meta's media API, parsed, and deduped.
  - **Gmail:** ICS attachments in incoming emails are extracted, downloaded via Gmail API, parsed, and deduped.
  - **Forward-to email:** ICS attachments in forwarded emails are extracted from the inbound webhook payload, parsed, and deduped.
  - **Confirmation:** A single batch confirmation ("Found N events — add all?") is sent via WhatsApp buttons. One PendingAction covers the whole batch.
  - **Confidence:** ICS data is treated as reliable (confidence 0.9), same as feed polling.
  - **No persistence:** Raw ICS content is parsed and discarded in the request — never stored.

### 4.6 Work Calendar Free/Busy (Deferred)

- **Mechanism:** Google Calendar FreeBusy API or Outlook equivalent.
- **Purpose:** See caregiver availability windows without exposing meeting details.
- **Status:** Deferred. Tracked in [Deferred Features](#19-deferred-features).

### 4.7 SMS / iMessage (Deferred)

- **Status:** Deferred to v2. Workaround: caregivers forward relevant texts to the WhatsApp group.

---

## 5. Data Models

### 5.1 Family (Tenant)

```
Family {
  id: uuid (primary key)
  whatsapp_group_id: string (unique)
  created_at: timestamp
  onboarding_complete: boolean
  forward_email: string (unique, e.g. "family-{id}@radar.app")
  settings: {
    daily_digest_time: time (default: 07:00 local)
    weekly_summary_day: day (default: Sunday)
    weekly_summary_time: time (default: 09:00 local)
    timezone: string
  }
}
```

### 5.2 Caregiver

```
Caregiver {
  id: uuid
  family_id: uuid (FK → Family)
  whatsapp_phone: string
  name: string
  google_account_email: string (nullable — not all caregivers connect Google)
  google_refresh_token: encrypted_string (nullable)
  google_token_expires_at: timestamp (nullable)
  gmail_watch_expiry: timestamp (nullable)
  gcal_watch_expiry: timestamp (nullable)
  joined_at: timestamp
  is_active: boolean
}
```

### 5.3 Child

```
Child {
  id: uuid
  family_id: uuid (FK → Family)
  name: string
  age: integer (derived from date_of_birth)
  date_of_birth: date (nullable)
  school: string (nullable, learned)
  grade: string (nullable, learned)
  activities: [string] (learned, e.g. ["soccer", "piano", "swim"])
  friends: [{name, parent_contact}] (learned)
  gear_inventory: [{item, size, last_updated}] (learned)
  notes: text (learned, freeform)
}
```

### 5.4 Event

```
Event {
  id: uuid
  family_id: uuid (FK → Family)
  source: enum (email | calendar | manual | ics_feed | forwarded)
  source_refs: [string] (dedup: list of source IDs that mapped to this event)
  type: enum (
    birthday_party | sports_practice | sports_game | school_event |
    camp | playdate | medical_appointment | dental_appointment |
    recital_performance | registration_deadline | other
  )
  title: string
  description: text (nullable)
  children_involved: [uuid] (FK → Child)
  datetime_start: timestamp
  datetime_end: timestamp (nullable)
  location: string (nullable)
  location_coordinates: point (nullable)

  # Recurrence
  is_recurring: boolean (default false)
  recurring_schedule_id: uuid (nullable, FK → RecurringSchedule)

  # RSVP
  rsvp_status: enum (pending | accepted | declined | not_applicable)
  rsvp_deadline: timestamp (nullable)
  rsvp_method: enum (reply_email | click_link | form | phone | not_applicable)
  rsvp_contact: string (nullable)

  # Preparation
  prep_tasks: [{
    task: string,
    status: enum (pending | complete),
    assigned_to: uuid (nullable, FK → Caregiver),
    due_date: timestamp (nullable)
  }]

  # Transportation
  transport: {
    drop_off_by: uuid (nullable, FK → Caregiver),
    pick_up_by: uuid (nullable, FK → Caregiver),
    notes: string (nullable)
  }

  # Metadata
  created_at: timestamp
  updated_at: timestamp
  extraction_confidence: float (0.0–1.0, from Email Extraction Agent)
  confirmed_by_caregiver: boolean (default false)
}
```

### 5.5 Recurring Schedule

A generalized model for any activity that repeats on a pattern over a bounded time period. Covers sports seasons, weekly music lessons, tutoring sessions, swim classes, after-school programs, religious education, and any other recurring activity.

```
RecurringSchedule {
  id: uuid
  family_id: uuid (FK → Family)
  child_id: uuid (FK → Child)
  activity_name: string (e.g. "soccer", "piano lessons", "swim class")
  activity_type: enum (sport | music | academic | social | medical | other)
  pattern: string (e.g. "every Tuesday and Thursday, 4:00–5:30pm")
  location: string
  start_date: date
  end_date: date
  confirmed: boolean (caregiver confirmed pattern detection)
  transport_pattern: {
    default_drop_off: uuid (nullable, FK → Caregiver),
    default_pick_up: uuid (nullable, FK → Caregiver)
  }
  exceptions: [{
    original_date: date,
    type: enum (cancelled | rescheduled | location_change | makeup),
    new_date: date (nullable),
    new_location: string (nullable),
    reason: string (nullable)
  }]
}
```

### 5.6 Action Item (non-event actionables extracted from emails)

```
ActionItem {
  id: uuid
  family_id: uuid (FK → Family)
  event_id: uuid (nullable, FK → Event — if related to a specific event)
  source: enum (email | manual | inferred)
  source_ref: string (nullable)
  type: enum (
    form_to_sign | payment_due | item_to_bring | item_to_purchase |
    registration_deadline | rsvp_needed | contact_needed | other
  )
  description: string
  due_date: timestamp (nullable)
  status: enum (pending | complete | dismissed)
  assigned_to: uuid (nullable, FK → Caregiver)
  children_involved: [uuid] (FK → Child)
  created_at: timestamp
  completed_at: timestamp (nullable)
}
```

### 5.7 Conversation Memory

```
ConversationMemory {
  id: uuid
  family_id: uuid (FK → Family)
  type: enum (short_term | long_term_summary)
  content: text
  embedding: vector (for semantic retrieval)
  created_at: timestamp
  expires_at: timestamp (nullable — short_term entries expire)
}
```

### 5.8 Family Learning Entry

```
FamilyLearning {
  id: uuid
  family_id: uuid (FK → Family)
  category: enum (child_school | child_activity | child_friend | contact |
                   gear | preference | schedule_pattern | budget)
  entity_type: enum (child | caregiver | external_contact)
  entity_id: uuid (nullable)
  fact: string (e.g. "Emma goes to Lincoln Elementary")
  source: string (e.g. "Extracted from email — Lincoln Elementary Newsletter, March 12")
  confidence: float (0.0–1.0)
  confirmed: boolean (default false — set true after weekly summary review)
  surfaced_in_summary: boolean (default false)
  created_at: timestamp
}
```

---

## 6. Agent Specifications

### 6.1 Email Extraction Agent

**Purpose:** Convert raw email content into structured Event and ActionItem records.

**Model strategy:**
- **Tier 1 (Triage):** Claude Haiku. Binary classification: "Is this email relevant to any family member's activities, events, or scheduling?" Covers kids' activities, adult/parent events (dinner reservations, concerts, travel), and shared family logistics. ~80% of emails are irrelevant. Discard irrelevant emails immediately.
- **Tier 2 (Extraction):** Claude Sonnet. For relevant emails, extract structured data into Event and ActionItem schemas. Extraction produces detailed descriptions including preparation checklists (using ☐ format) that can be tracked and updated later via conversation.

**Extraction scope (broader than just events):**
- Events: parties, practices, games, school events, appointments, recitals, camps
- Action items: forms to sign, payments due, items to bring, waivers, registration deadlines
- Schedule changes: time changes, cancellations, location changes for existing events
- Recurring patterns: recurring activity schedules embedded in emails
- Child references: fuzzy match names against family's Child records
- Contact information: other parents' names and contact info
- Timezone inference: datetimes are extracted in ISO 8601 format with timezone offset, inferred from event location (e.g., Tempe AZ → America/Phoenix). Family timezone is used as fallback.

**Deduplication:** Before creating a new Event, query the Event Registry for fuzzy matches:
- Match criteria: `datetime_start` within ±30 minutes AND title similarity > 0.7 (cosine similarity on embeddings or simple token overlap)
- If match found: merge — email-extracted data enriches the existing record. Email is the richer source for prep, RSVP, and action item data.
- If datetime conflicts between email and calendar source: flag to group, do not silently resolve.

**Event confirmation flow:** Extracted events are not auto-persisted. Instead, each event creates a `PendingAction` (type: `event_confirmation`) and sends a WhatsApp interactive button message with Yes/No buttons to the family group. The event is only created in the Event Registry when a caregiver confirms. Action items and learnings from the same email are still auto-persisted (AUTO mode).

**Confidence scoring:** Each extraction includes a confidence score (0.0–1.0). Events below 0.6 confidence are surfaced with an explicit "Is this right?" prompt. Events above 0.6 are surfaced with implicit correction opportunity.

**Feedback loop:** When a caregiver corrects an extraction (wrong date, wrong child, false positive), log the correction as a (raw_email, wrong_extraction, correct_extraction) triple for future model improvement.

### 6.2 Calendar Change Detector

**Purpose:** Process GCal webhook notifications, classify changes, and update the Event Registry.

**Change classification:**
- `new_event`: New event on any connected calendar → create Event in local DB, check for duplicates
- `time_change`: Existing event rescheduled → update Event in local DB
- `cancellation`: Event deleted → update Event status in local DB, if part of season log as exception
- `location_change`: Location updated → update Event in local DB
- `attendee_change`: New attendees added → check if new child involved

**No WhatsApp notifications for GCal changes:** GCal is the source of truth. Changes made directly in GCal (adds, updates, deletions) are synced to the local Event Registry but do NOT generate WhatsApp notifications — the user already knows about changes they made in their own calendar. WhatsApp notifications for events come only from email ingestion (via the Email Extraction Agent → pending action approval flow).

**Recurring schedule awareness:** If a recurring event instance is modified or cancelled, update only that instance in the RecurringSchedule exceptions list. Do not modify the overall schedule pattern.

**ICS feed processing:** Same change detection logic applied to diffed ICS feed data on each poll cycle.

### 6.3 Intent Router (Orchestrator)

**Purpose:** Classify inbound WhatsApp messages and route to the appropriate agent or action.

**Intent categories:**

| Intent | Example | Routes to |
|--------|---------|-----------|
| `query` | "What's on Saturday?" | Calendar Coordinator |
| `schedule` | "Set up a playdate with Max" | Calendar Coordinator |
| `prepare` | "What do we need for soccer?" | Logistics Planner |
| `research` | "Find summer camps for Emma" | Research Agent |
| `edit_instruction` | "Make it more casual" | Regenerate pending draft |
| `approve` | "Send it", "yes", "looks good" | Execute pending action |
| `assignment_claim` | "I'll take Jake" | Update transport assignment |
| `update` | "Practice moved to 4pm" | Calendar Coordinator |
| `correction` | "Actually that's next Saturday" | Update Event/ActionItem |
| `event_update` | "I already bought the wedding gift" | Match event from context/GCal, update description |
| `dismiss` | "Skip", "not interested" | Dismiss pending suggestion |
| `share_info` | "My son is John", "Emma goes to Lincoln Elementary" | Create/update child records, store as learning |
| `general` | "Thanks", "ok" | Acknowledge, no routing |

**Button reply handling:** When a WhatsApp message contains a button reply (interactive message type `button_reply`), the router decodes the structured button ID (`{action_type}:{pending_action_id}:{response}`) and routes directly to the approval handler with confidence=1.0 — no LLM classification needed. This bypasses the keyword and LLM classification pipeline entirely.

**Open conversation state:** When a SUGGEST-mode action is pending approval, the router enters a "pending approval" state for that family. In this state:
- Any reply is classified as `edit_instruction`, `approve`, or `dismiss`.
- If ambiguous, the bot asks for clarification: "Want to change something, or should I send it?"
- The state persists until the action is explicitly approved or dismissed.
- Other intents (new queries, new events) can still be processed — pending approvals don't block the conversation.

**Conversation context:** The classifier receives the last 10 messages from conversation memory to understand follow-up messages. If a user just confirmed "Garden Party for the Newlyweds" and then says "I already bought a wedding gift," the classifier recognizes this as an `event_update` for the garden party.

**Smart event matching (two-tier context):** The `cancel_event`, `modify_event`, and `event_update` handlers all use the same two-tier strategy to identify which event the user is referring to:
1. **Tier 1 — Recent conversation:** Check the last 10 messages for context about which event the user means.
2. **Tier 2 — GCal search:** Query Google Calendar for upcoming events (90-day default window) and let the LLM fuzzy-match the message against event titles and descriptions.
3. The LLM matches the message to an event, determines the action, executes it (cancel from GCal, modify in GCal, or update description), and confirms to the user.

- **cancel_event:** Deletes the matched event from GCal and soft-deletes locally (appends `[CANCELLED]` to description).
- **modify_event:** Applies the requested changes (time, location, title, etc.) to both GCal and local DB.
- **event_update:** Updates event metadata (e.g., mark a prep checklist item as done: ☐ → ☑) in both GCal and local DB.

**Schedule queries use GCal as source of truth:** When a user asks "what's on my schedule," the system queries Google Calendar directly (not the local Event Registry) to ensure manually-added events and external changes are included. Falls back to local DB if GCal is unavailable.

**Voice note handling:** Audio messages from WhatsApp are first sent to Whisper API for transcription, then the transcript is processed through the same intent classification pipeline as text messages.

### 6.4 Calendar Coordinator Agent

**Purpose:** All scheduling, conflict detection, and caregiver calendar synchronization.

**Capabilities:**

- **Event creation:** When a new event is confirmed (via button tap or text reply), create the event in the local Event Registry AND write to Google Calendar on all connected caregivers' calendars. GCal is the source of truth. After confirmation, concise prep tips are sent to the caregiver based on the event description. AUTO.
- **Conflict detection:** Before adding any event, check all caregivers' calendars for overlapping time blocks. Surface conflicts to the group.
- **Cross-child conflicts:** If two children have overlapping events at different locations, surface with transport implications: "Jake has soccer at 3pm and Emma has piano at 3:30pm — different locations. Who's taking whom?"
- **Playdate scheduling:** Manages the flow: check family availability → suggest times → draft message to other parent (SUGGEST mode) → track response.
- **Recurring schedule management:** After recurring schedule pattern is confirmed, auto-create individual Event instances for each occurrence.

### 6.5 Logistics Planner Agent

**Purpose:** Everything that needs to happen before and around events.

**Capabilities:**

- **Prep checklist generation:** Based on event type, auto-generate ActionItems:
  - Birthday party → gift needed, RSVP, check if ride needed
  - Sports season start → gear check (reference child's gear_inventory), registration forms
  - School event → permission slips, payments, items to bring
  - Camp → registration, medical forms, packing list
- **Gear tracking:** Maintain gear_inventory on Child records. Surface gaps at schedule transitions: "Does Jake still have cleats that fit? He was size 3 last season."

#### 6.5.1 Transport Coordination

**Purpose:** Coordinate which caregiver handles drop-off and pick-up for child events.

**Gating — when transport coordination applies:**

Transport coordination only runs when ALL of these are true:
- The family has at least one child (`family.children` is non-empty)
- The event has a `child_id` (it's a child event, not a parent/family event)
- The family has 2+ active caregivers

If the family has only 1 active caregiver, transport is auto-assigned to that caregiver silently — no prompts, no "who's handling?" questions. If the family has no children, the entire transport subsystem is skipped.

**Transport assignment on event creation:**

When an event is created with a `child_id`:
1. Check if the event matches a `RecurringSchedule` that has confirmed transport routines (see Routine Inference below).
2. If match found → auto-populate `drop_off_by` and/or `pick_up_by` from the routine defaults.
3. If no match → leave transport unassigned. The event enters the unclaimed transport reminder pipeline.
4. After any assignment (auto or claimed), run the sibling conflict check.

**Claiming transport via conversation:**

Drop-off and pick-up are independent assignments — they can be claimed by different caregivers. Caregivers assign themselves via free text:
```
Caregiver A: I'll drop Emma at soccer
Bot: ✓ You're on drop-off for Emma's soccer Tuesday at 4pm. Pick-up is still unassigned.

Caregiver B: I can grab her after
Bot: ✓ You're on pick-up for Emma's soccer Tuesday at 4pm.
```

If a caregiver doesn't specify a role, it defaults to "both":
```
Caregiver: I'll take Emma to soccer
Bot: ✓ You're on drop-off and pick-up for Emma's soccer Tuesday at 4pm.
```

The `assign_transport` intent extracts child name, event hint, and role (drop_off, pick_up, or both) using the `ExtractedAssignment` schema. Matching uses the same two-tier context strategy as other event handlers (conversation context → GCal search).

**Swap flow — releasing a routine assignment:**

When a caregiver can't cover their usual assignment:
```
Caregiver: I can't do pickup Thursday
Bot: Got it — Thursday soccer pickup is now unassigned.
     [to all caregivers]: Thursday soccer pickup for Jake needs someone. Who can cover?
```

The swap clears the assignment on that **specific event instance only** — the routine default stays intact for future weeks. The event then enters the normal unclaimed transport reminder pipeline.

**Sibling conflict detection:**

Triggered any time transport is assigned (auto-populated from routine or claimed by caregiver). Drop-off and pick-up are checked independently — a caregiver can drop off one child and pick up another if the times don't overlap. Checks for:
- Another event for a **different child** in the same family
- With **overlapping time** (±30 minute window)
- At a **different location**
- Where the **same caregiver** is assigned to the **same role** (both doing drop-off, or both doing pick-up) for both events

If conflict found, flag it to all caregivers:
```
Bot: Heads up — Mom is assigned to drop off Emma at soccer (4:00 PM, Fieldhouse)
     and drop off Jake at piano (4:15 PM, Music Center). One of these needs a different driver.
```

The system flags the conflict only. It does not propose which caregiver should swap.

**Unclaimed transport reminder pipeline (fixed cadence):**

| Timing | Channel | Message |
|--------|---------|---------|
| Daily digest (morning) | Digest line item | "Soccer at 4pm Tuesday — drop-off: Mom, pick-up: unassigned" |
| 48 hours before event | Direct message to all caregivers | "Jake's soccer tomorrow at 4pm still needs pick-up assigned. Who's handling it?" |
| 4 hours before event | Urgent message to all caregivers | "⚠️ Jake's soccer at 4pm TODAY still has no pick-up assigned!" |

Escalation cadence is fixed — not configurable per family.

#### 6.5.2 Transport Routine Inference

**Purpose:** Learn recurring transport patterns from caregiver behavior, rather than asking upfront.

**Mechanism:** Uses the FamilyLearning lifecycle (see Section 11).

1. **Track claims:** Each time a caregiver claims transport for an event tied to a `RecurringSchedule`, record the tuple: (caregiver, recurring_schedule, day_of_week, role). Drop-off and pick-up are tracked as **separate tuples** — a family may have one caregiver who always drops off and a different one who always picks up.
2. **Detect pattern:** After 3 consistent claims by the same caregiver for the same (recurring_schedule, day_of_week, role), create a `FamilyLearning` entry with `confirmed = false`:
   - Category: `transport_routine`
   - Value: "Mom handles Tuesday soccer drop-off" or "Grandma handles Tuesday soccer pick-up"
   - Each role generates its own independent learning entry
3. **Surface for confirmation:** In the next weekly Sunday summary:
   ```
   Bot: I've noticed some transport patterns:
        • Mom always handles Tuesday soccer drop-off
        • Grandma always handles Tuesday soccer pick-up
        I'll keep assigning those automatically. Correct me if anything changes.
   ```
4. **Confirm or correct:**
   - No correction → `confirmed = true`. Future events from that recurring schedule auto-populate `drop_off_by` and/or `pick_up_by` from the respective routine.
   - Correction ("actually Dad does Tuesday drop-off now") → update that specific learning entry, reset the claim counter for the old caregiver, begin tracking the new one. The other role's routine is unaffected.
5. **Write defaults:** When a transport routine learning is confirmed, update `RecurringSchedule.default_drop_off_caregiver` or `default_pick_up_caregiver` accordingly. These are independent fields — confirming a drop-off routine does not affect the pick-up default, and vice versa.

**No upfront questions:** The system never asks "who usually handles pickup?" during onboarding or recurring schedule creation. It observes and confirms passively.

### 6.6 Research Agent

**Purpose:** External information retrieval for camps, gifts, activities, venues.

**Capabilities:**

- **Gift recommendations:** Based on birthday child's age and family's learned budget range. Present 3 options via WhatsApp list message. "Other ideas" re-runs with different parameters.
- **Camp/activity discovery:** Search for options matching child age, family location, target dates, budget. Ranked results with key details.
- **Venue lookup:** Party venues, activity providers, sports facilities near the family's location.

**All outputs are SUGGEST mode** — require caregiver approval before any action is taken.

### 6.7 Reminder Engine

**Purpose:** Scheduled notification system. Not an LLM agent — a cron-based evaluator that uses LLM for natural language generation of reminder messages.

**Three notification types:**

1. **Daily digest** — Evaluates the Event Registry and ActionItem list each morning.
   - Fires ONLY when there's something actionable. Skips entirely on empty days.
   - Content: today's events with prep status, approaching deadlines, unclaimed assignments.
   - Delivered via WhatsApp template message.

2. **Weekly Sunday summary** — Always fires, regardless of content.
   - Content: week ahead overview, family learnings to review (from FamilyLearning entries where `surfaced_in_summary = false`), recurring schedule updates, prep status across upcoming events.
   - Delivered via WhatsApp template message.

3. **Immediate triggers** — Fire in real-time, regardless of digest schedule.
   - New event detected with short RSVP window (< 48 hours to deadline)
   - Scheduling conflict detected
   - Unclaimed transport assignment with event approaching (context-aware timing)
   - Recurring schedule exception detected (practice cancelled, rescheduled)

---

## 7. Interaction Patterns

All SUGGEST-mode actions use one of five interaction patterns. The bot uses **open conversation state** — any reply to a pending suggestion is classified as edit instruction, approval, or dismissal. For binary decisions (Type 1), WhatsApp interactive buttons are used. For all other patterns, text-based replies are used.

### 7.1 Type 1: Binary Approval (Interactive Buttons)

**Use case:** Simple yes/no decisions with no content to review.
**Examples:** Add event to calendar, confirm an extraction.

**Mechanism:** WhatsApp interactive button messages with Yes/No buttons. Button taps are routed directly via encoded button IDs (no LLM classification needed, confidence=1.0). Text replies ("yes"/"no") also work as a fallback and route through the standard intent classifier.

**Button ID format:** `{action_type}:{pending_action_id}:{response}` — e.g., `event_confirm:a1b2c3d4-...:yes`

```
Bot: [Interactive button message]
     📬 New event from email:
     *Sophia's 7th Birthday*
     📅 Fri Mar 28, 02:00 PM
     📍 JumpZone
     [Yes] [No]

Caregiver: [taps Yes]

Bot: ✅ Added to your calendar: *Sophia's 7th Birthday*
     Fri Mar 28, 02:00 PM
     📍 JumpZone

     *Heads up:*
     • Purchase and wrap birthday gift ($30-40 range)
     • RSVP to Sophia's mom by March 25
     • Pack socks for JumpZone (required)
```

Each extracted event creates a `PendingAction` (type: `event_confirmation`) with the event data stored in the action's `context` field. The event is only persisted to the Event Registry AND Google Calendar when a caregiver taps Yes or replies "yes". After confirmation, concise prep tips are generated from the event description and sent to the caregiver.

### 7.2 Type 2: Content Approval

**Use case:** Caregiver reviews a drafted message before it goes out.
**Examples:** RSVP emails, playdate invites, coach messages.

```
Bot: Here's a draft RSVP to Sophia's mom:

     "Hi! Emma would love to come to Sophia's party.
      See you on the 28th! — Sarah"

     Reply to edit, or say send to approve.

Caregiver: make it warmer, add that she's excited

Bot: Updated:
     "Hi! Emma is SO excited for Sophia's party — she's
      been counting down the days! See you on the 28th! — Sarah"

Caregiver: send it

Bot: Sent ✓
```

**Edit loop:** Can repeat as many times as needed. Ambiguous replies trigger clarification.

### 7.3 Type 3: Options

**Use case:** Caregiver picks from multiple choices.
**Examples:** Gift suggestions, camp options, venue alternatives.

```
Bot: Gift ideas for Sophia (turning 7, ~$30–40):

     1. Melissa & Doug art kit — $35
     2. National Geographic science kit — $32
     3. Creativity for Kids jewelry set — $28

     Reply with a number, or say "more ideas"

Caregiver: 2

Bot: Added to prep checklist: National Geographic science kit
     Amazon link: [link]
```

**"More ideas"** re-runs the Research Agent with different parameters.

### 7.4 Type 4: Assignment

**Use case:** Specific caregiver needs to claim a task.
**Examples:** Transportation, purchasing, form submission.

```
Bot: Jake has soccer Saturday at 3pm (Westfield Fields)
     and Emma has piano at 3:30pm downtown — same time,
     different places.

     Who's taking Jake? Who's taking Emma?

Caregiver 1: I'll take Jake

Caregiver 2: I got Emma

Bot: Got it — Sarah: Jake's soccer. Mike: Emma's piano.
     Added to both events ✓
```

**Nudge behavior:** If no one claims the task and the event is approaching, the bot sends a reminder to the whole group. Timing is context-aware (event in 4 hours = urgent nudge; event in 5 days = gentle reminder).

### 7.5 Type 5: Deadline Urgency

**Use case:** Time-sensitive action needed.
**Examples:** Registration closing, RSVP expiring.

```
Bot: ⚠️ Camp Thunderbird registration closes tomorrow.

     Emma: age 9 ✓  Dates: July 7–18 ✓  Cost: $450

     Want the registration link? Or I can remind you
     tomorrow morning.

Caregiver: send me the link

Bot: Here: [registration link]
     I'll remind you at 9am if it's not done.
```

The bot does not fill out forms or handle payment. It surfaces the right link at the right moment and follows up.

---

## 8. Notification System

### WhatsApp Template Messages (require pre-approval)

Templates needed for proactive (bot-initiated) messages:

| Template ID | Purpose | Variables |
|-------------|---------|-----------|
| `new_event` | New event detected | `{event_title}`, `{date}`, `{child_name}`, `{action_prompt}` |
| `reminder` | Event reminder | `{event_title}`, `{timeframe}`, `{prep_note}` |
| `deadline_alert` | Approaching deadline | `{action_description}`, `{deadline}` |
| `approval_request` | Action needs approval | `{action_description}`, `{details}` |
| `daily_digest` | Morning briefing | `{summary}` |
| `weekly_summary` | Sunday overview | `{summary}` |
| `assignment_nudge` | Unclaimed task | `{task_description}`, `{event_time}` |
| `conflict_alert` | Schedule conflict | `{event_1}`, `{event_2}`, `{conflict_description}` |

### 24-Hour Window Rule

Once any caregiver responds to a bot message, a 24-hour free-form conversation window opens. Design flows to start with a template (triggering the window), then continue with natural conversation within the window.

---

## 9. Onboarding

### Flow

Fully conversational. Web UI exists only for Google OAuth token exchange.

**Step 1: Bot added to group**
```
Bot: 👋 Hi! I'm Radar, your family's activity assistant.
     Quick setup — what are your kids' names and ages?
```

**Step 2: Family profile initialized**
```
Caregiver: Emma, 9 and Jake, 7

Bot: Got it — Emma (9) and Jake (7).
     I'll send each person in this group a link to connect
     your Google calendar and email. That's what lets me
     spot events automatically.

     Sarah: [OAuth link]
     Mike: [OAuth link]

     If you'd prefer not to connect your email, you can
     also forward things to: family-abc123@radar.app
```

**Step 3: OAuth completion**
```
Bot: Both accounts connected ✓
     I'll start picking up events from your inboxes.
     Tell me anything else about the kids as we go —
     I'll learn the rest over time.
```

### Requirements

- Onboarding completes in 3 exchanges maximum.
- OAuth link opens a minimal web page that handles the Google OAuth redirect and token storage. No other web UI features at MVP.
- Caregivers who don't complete OAuth can still participate in the group and interact with the bot. They just don't contribute email/calendar data.
- The family's forward-to email address is generated at tenant creation and communicated during onboarding.

---

## 10. Recurring Schedules

Recurring schedules are a generalized model for any activity that repeats on a pattern over a bounded time period. This covers sports seasons, weekly music lessons, tutoring sessions, swim classes, after-school programs, dance classes, religious education, and any other recurring activity.

### Detection

**From email:** If an email contains an explicit recurring schedule (e.g., "piano lessons every Wednesday, 3:30–4:30pm, September through May" or "practice every Tuesday/Thursday, March 4 – May 22"), the Email Extraction Agent extracts the full pattern and creates a RecurringSchedule record.

**From calendar:** If the Calendar Change Detector observes 3+ consecutive events with the same title, similar time, and same location, it infers a recurring pattern.

### Confirmation

In both cases, the bot confirms with the group before tracking as a recurring schedule:

```
Bot: Looks like Jake has soccer practice every Tuesday and
     Thursday, 4–5:30pm at Westfield Fields through May 22.
     Should I track this as a recurring schedule?

Caregiver: yes

Bot: Done — Jake's soccer tracked through May 22.
     I'll manage the schedule and let you know about
     any changes.
```

```
Bot: It looks like Emma has piano lessons every Wednesday,
     3:30–4:30pm at Miller Music Studio. When does this
     series end?

Caregiver: end of May

Bot: Got it — Emma's piano lessons tracked through May 31.
```

### Exception Handling

- **Cancellation:** Remove the instance, log exception. Report in next daily digest.
- **Reschedule:** Update the instance datetime, log exception. Check for conflicts at new time.
- **Location change:** Update the instance location. Notify group if transport implications.
- **Makeup/extra session:** Create a new Event instance linked to the recurring schedule. Check for conflicts.

Exceptions are handled silently (no confirmation needed) and reported in the next daily digest.

### Schedule End

When the last event in a recurring schedule passes:
```
Bot: Jake's soccer season ended this week. Gear to return:
     shin guards (borrowed from league). Want me to archive
     this schedule?
```

```
Bot: Emma's piano lesson series ends this week. Want to
     renew for next term?
```

---

## 11. Family Knowledge & Preferences

### Principle

Radar continuously builds a picture of the family from emails, calendar events, and conversations. This knowledge falls into two categories:

1. **Facts** — what Radar knows about the family (schools, friends, contacts, schedules)
2. **Preferences** — how the family wants Radar to behave (communication style, scheduling rules, delegation, decision defaults)

Knowledge is stored, surfaced for correction, and — critically — **fed back into agent prompts** so it actually improves Radar's behavior over time.

### Three-Tier Storage Model

Data lives in one of three tiers based on whether the system needs to act on it programmatically:

#### Tier 1: Structured Core (typed columns, deterministic behavior)

Data that gates system behavior or has a natural home in an existing table. Code queries and acts on these directly — no LLM interpretation needed.

| Table | What it stores |
|-------|---------------|
| `children` | Name, DOB, school, grade, activities |
| `child_friends` | Friend name, parent name, contact |
| `families` | Timezone, digest time, summary day/time |
| `caregivers` | Name, phone, email |
| `caregiver_preferences` | Quiet hours, delegation areas |

When a learning is confirmed and has a structured target (e.g., "Emma goes to Lincoln Elementary" → `children.school`), it **graduates** from the staging area into the structured table.

#### Tier 2: Freeform Preferences (strings injected into LLM prompts)

Preferences too nuanced or varied to structure. Stored as `family_learnings` rows with `pref_*` categories. The LLM reads them as prompt context and adjusts behavior naturally.

Examples:
- "Keep messages short" (`pref_communication`)
- "We never schedule activities on Sunday mornings" (`pref_scheduling`)
- "Remind me about gifts 3 days before events" (`pref_prep`)
- "Dad prefers to handle sports logistics" (`pref_delegation`)
- "Default birthday gift budget is around $30" (`pref_decision`)
- "Always RSVP yes for Emma's close friends" (`pref_decision`)

#### Tier 3: Staging Area (observations awaiting graduation)

`family_learnings` also serves as a staging area for factual observations extracted from emails. These land here first, then graduate to structured tables when confirmed:

- "Emma goes to Lincoln Elementary" → on confirm, update `children.school`
- "Jake's friend Max, mom is Lisa Chen" → on confirm, create `ChildFriend` row
- "Emma does swim" → on confirm, append to `children.activities`

After graduation, the learning row is marked `graduated = true` — history is preserved but it's not surfaced again.

### What the Bot Learns

**Facts** (graduate to structured tables when confirmed):
- Child's school (from school email domains, newsletter headers)
- Child's activities and teams (from registration emails, season schedules)
- Child's friends (from playdate requests, party invites)
- Other parents' contact info (from email metadata)
- Coaches and teachers (from email senders)
- Gear inventory (from purchase confirmations, "need new cleats" conversations)
- Transport routines (Mom usually does Tuesday soccer pickup, Dad does Thursday piano drop-off) — inferred from 3 consistent claims, confirmed via weekly summary (see Section 6.5.2)
- Budget norms (typical gift spending range, camp budget)

**Preferences** (stay as freeform strings or structured fields):

| Category | Scope | Examples |
|----------|-------|---------|
| `pref_communication` | Per-caregiver | "Keep messages short", "I like detail", "Use bullet points" |
| `pref_scheduling` | Per-family | "No activities on Sundays", "Prefer morning activities", "Max 2 weeknight events" |
| `pref_notification` | Per-caregiver | Quiet hours (structured), "Don't ping me for RSVP reminders" |
| `pref_prep` | Per-family | "Remind about gifts 3 days before", "I always pack gear the night before" |
| `pref_delegation` | Per-caregiver | Delegation areas (structured), "Mom handles school, Dad handles sports" |
| `pref_decision` | Per-family | "Default gift budget $30", "Always RSVP yes for close friends" |

### Detection Sources

| Source | Mechanism | Confirmation required? |
|--------|-----------|----------------------|
| Explicit chat statement | "Don't message me before 7am" | No — direct instruction, stored as `confirmed = true` |
| Email extraction | Budget norms from camp registration email | Yes — surfaced in weekly summary |
| Light behavioral inference | "I noticed you usually decline weeknight events" | Yes — posed as a question, only stored if caregiver confirms |
| Onboarding | "What time do you want your daily digest?" | No — direct answer |

**Light inference rules:** The bot may observe repeated patterns (3+ occurrences over 2 weeks) and surface them as suggestions in the weekly summary. Inferred preferences are never stored without explicit confirmation. Examples:
- "I noticed Mom usually handles Tuesday pickups — should I remember this?"
- "You've declined the last 3 weeknight events — want me to flag those automatically?"

### Learning Lifecycle

```
Detection (email, conversation, inference)
       ↓
family_learnings (confirmed = false, surfaced_in_summary = false)
       ↓ surfaced in weekly Sunday summary
family_learnings (surfaced_in_summary = true)
       ↓ no correction received by next summary cycle
family_learnings (confirmed = true)
       ↓ if structured target exists (GRADUATION_MAP)
Structured table updated (children.school, child_friends, etc.)
family_learnings (graduated = true)
```

**Exception — explicit statements:** When a caregiver directly states a preference or fact in conversation, it's stored immediately as `confirmed = true` with no summary cycle needed.

### Per-Caregiver vs. Per-Family

- `family_learnings.caregiver_id = NULL` → family-wide (applies to everyone)
- `family_learnings.caregiver_id = <UUID>` → per-caregiver (overrides family-wide for that person)

Structured preferences (`caregiver_preferences` table) are always per-caregiver.

### Correction Handling

Caregivers can correct at any time, not just during summaries. The router classifies corrections as `correct_learning` intent.

**Correcting a structured fact:**
```
Caregiver: Actually Emma goes to Washington Elementary, not Lincoln

Bot: Updated ✓ — Emma's school changed to Washington Elementary.
```
→ Updates `children.school` directly (or the learning if not yet graduated).

**Correcting a freeform preference:**
```
Caregiver: Actually, remind me about gifts a week before, not 3 days

Bot: Got it — updated to remind about gifts a week before events.
```
→ Old learning is superseded (not deleted — history preserved), new one created as confirmed.

### How Preferences Flow Into Agent Behavior

Every agent that builds an LLM prompt queries confirmed learnings and active preferences for the family (and caregiver, where applicable) via the shared context builder. The context includes:

- Confirmed facts not yet graduated (e.g., "Coach Johnson runs the swim team")
- Active freeform preferences (e.g., "Keep messages short", "No Sunday activities")
- Structured preferences are used directly in code (quiet hours gate message sending, delegation areas route notifications)

Preferences don't require hard-coded logic for each variation — they're injected into prompts and the LLM applies them naturally. The structured preferences (quiet hours, delegation) are the exception: these gate system behavior before any LLM is called.

---

## 12. Proactive Gap Detection

### Purpose

Go beyond reminders for existing events. Identify things the family should be doing but isn't — missing appointments, unscheduled activities, opportunities.

### Detection Rules

| Gap Type | Signal | Action |
|----------|--------|--------|
| Missed medical/dental | No dental appointment in 7+ months per child | Suggest scheduling |
| Playdate drought | No playdate for a child in 3+ weeks | Suggest reaching out to recent contacts |
| Upcoming school break | School break detected on calendar with no camp/activity scheduled | Surface camp discovery options |
| Activity registration | Known recurring activity approaching (based on prior year data) | Alert about registration windows |
| Stale RSVP | Event RSVP status is `pending` and deadline is within 48 hours | Urgent reminder |
| Uncompleted prep | Event has pending prep tasks within 48 hours of event | Escalated reminder with checklist status |
| Birthday approaching | Child's friend birthday within 2 weeks, no party invite received | "Has [friend] mentioned a birthday party? Want me to check?" |

### Implementation

The gap detection engine runs as part of the Reminder Engine's daily evaluation job. It queries the Event Registry, ActionItem list, Family Profiles, and RecurringSchedule records to identify gaps, then generates natural-language messages surfaced through appropriate notification channels.

All gap detection outputs are SUGGEST mode — they present options and ask, never auto-act.

---

## 13. Autonomy Matrix

| Action | Mode | Notes |
|--------|------|-------|
| Create/modify/delete calendar events | AUTO | On all connected caregiver calendars |
| Send WhatsApp messages to family group | AUTO | Reminders, digests, suggestions |
| Update Event Registry (from calendar) | AUTO | Calendar webhooks and ICS feeds |
| Update Event Registry (from email) | CONFIRM | Interactive Yes/No buttons; event created only on caregiver confirmation |
| Update Family Profiles | AUTO | Silent learning, weekly summary for review |
| Track recurring schedules and exceptions | AUTO | After initial confirmation |
| Auto-assign transport from routine | AUTO | Populates drop_off_by/pick_up_by from confirmed routines |
| Auto-assign transport (single caregiver) | AUTO | Silent, no prompts when only one caregiver exists |
| Flag sibling transport conflict | AUTO | Flags only, does not propose resolution |
| Nudge group for unclaimed transport | AUTO | Fixed cadence: digest → 48h → 4h escalation |
| Nudge group for unclaimed tasks | AUTO | Context-aware timing |
| Generate and update prep checklists | AUTO | Based on event type |
| Send emails to external parties | SUGGEST | Draft shown, caregiver approves |
| RSVP to invitations | SUGGEST | Draft shown, caregiver approves |
| Gift/equipment purchase links | SUGGEST | Options shown, caregiver selects |
| Camp/activity registrations | SUGGEST | Link shown, caregiver completes |
| Playdate coordination messages | SUGGEST | Draft shown, caregiver approves |
| Gap detection suggestions | SUGGEST | Presented as optional recommendations |

---

## 14. Multi-Tenancy

### Tenant Isolation

- **Tenant = Family = WhatsApp group.**
- **Database:** Row-level security in PostgreSQL. Every query filters by `family_id`. No cross-tenant data access under any circumstance.
- **Agent prompts:** Include only the current family's context. Never include data from other tenants in any LLM prompt.
- **API keys:** Per-tenant OAuth tokens stored encrypted. No shared credentials between tenants.

### OAuth Management

- Each caregiver's Google refresh token is encrypted at rest (AES-256, key in GCP Secret Manager or equivalent).
- Token refresh is handled automatically by the ingestion pipeline.
- If a token expires or is revoked, the bot notifies that specific caregiver in the group with a re-auth link.
- Token scope: Gmail read-only + Calendar read/write. Radar never has send/modify/delete access to caregiver email.

### Scaling

- Pub/Sub topics can be shared across tenants; messages include tenant ID for routing.
- GCal/Gmail watch channels are per-caregiver, tracked in the Caregiver table.
- WhatsApp webhook endpoint is shared; incoming messages are routed by group ID.

---

## 15. Security and Privacy

### Email Sending Architecture

Radar sends external emails (RSVPs, playdate messages, coach emails) from **its own domain**, not from the caregiver's Gmail account. This is a deliberate security decision.

- **Send address:** `{caregiver-name}@notifications.radar.app` or similar. The caregiver's name appears as the sender display name (e.g., "Sarah via Radar").
- **Send infrastructure:** Dedicated email sending service (SendGrid, Postmark, or AWS SES). Not Gmail API.
- **Gmail scope stays read-only.** Radar never has send, modify, or delete access to any caregiver's email account. This eliminates an entire class of security risks.
- **Safety window:** After a caregiver approves an external email, the bot waits 10 seconds before sending and displays: "Sending in 10 seconds... reply 'cancel' to stop." This prevents accidental sends from hasty approvals.
- **Full audit log:** Every sent email is logged with: full content, recipient address, which caregiver approved it, timestamp, and the original draft + edit history.

### Data Handling

- **Emails are processed ephemerally.** Extract structured data (Event, ActionItem, FamilyLearning), then discard raw email content. Never store full email bodies in the database.
- **Forward-to emails:** Same ephemeral processing. Raw email stored only in a processing queue with a 1-hour TTL.
- **Voice notes:** Transcribed, then audio deleted. Transcript processed as text and subject to same retention as conversation memory.
- **Calendar events:** Radar has read/write access to Google Calendar. Calendar writes use soft-delete with undo — when Radar removes a calendar event, it marks it cancelled in the Event Registry first and sends a notification. Hard delete only after caregiver confirmation.

### Threat Model

| Threat | Risk | Mitigation |
|--------|------|------------|
| **Incorrect email sent to wrong person** | Embarrassment, privacy breach | All external emails are SUGGEST mode with explicit recipient shown. 10-second cancel window after approval. Audit log of every sent email. Contradictory concurrent responses pause execution. |
| **Incorrect event created from misextracted email** | Caregiver shows up wrong place/time | Extraction confidence scoring (< 0.6 triggers explicit "Is this right?"). Correction feedback loop. High-precision, low-recall default. |
| **Prompt injection via email** | Malicious email manipulates LLM extraction to take unauthorized actions | Email content treated as untrusted data, never as LLM instructions. Extraction uses structured JSON output schema enforcement. Extraction output is data only — never executed as agent commands. |
| **Tenant data leakage** | Family A's data appears in Family B's context | Row-level security in PostgreSQL. family_id set at request boundary. All data queries scoped. LLM prompts constructed only from queried (tenant-scoped) data. No cross-tenant caching. |
| **OAuth token compromise** | Attacker gains read access to caregiver's email + read/write to calendar | AES-256 encryption at rest. Key in GCP Secret Manager (not env vars). Minimal scope: gmail.readonly + calendar.events only. Token revocation on detected breach. Periodic key rotation. |
| **Accidental calendar deletion** | Bot removes legitimate calendar events | Soft-delete with undo window. Bot notifies group before removing any calendar event. Hard delete only after explicit confirmation. |
| **Email account compromise via OAuth** | Attacker uses Radar's OAuth to access caregiver email | Gmail scope is read-only. Radar cannot send from, modify, or delete caregiver emails. Worst case: attacker reads emails, which requires both database breach AND decrypting the refresh token. |
| **Rate limit / abuse** | Bot sends excessive WhatsApp messages or emails | Per-family rate limits on outbound messages. Circuit breaker if error rates spike. Admin alerts on anomalous sending patterns. |

### Caregiver Controls

- `"Forget this"` command: Deletes a specific Event, ActionItem, or FamilyLearning entry and its source reference.
- `"What do you know about [child]?"` command: Bot surfaces all stored data about a child from Family Profiles, learned facts, and gear inventory.
- `"Pause"` command: Temporarily stops email/calendar processing. Bot remains in the group but only responds to direct messages.
- `"Delete our data"` command: Full tenant data deletion. Irreversible. Requires confirmation from the caregiver who initiated onboarding.

### Compliance Target

- SOC 2 Type II from launch if pursuing enterprise/school partnerships.
- COPPA considerations: the system processes data about children. No direct interaction with children. All data is managed by caregivers.

---

## 16. Technical Stack

| Component | Choice | Notes |
|-----------|--------|-------|
| Runtime | Python + FastAPI (async) | Best LLM ecosystem; async for webhook concurrency |
| Agent framework | LangGraph or Claude Agent SDK | Stateful multi-agent orchestration |
| LLM — triage | Claude Haiku | Email relevance classification |
| LLM — reasoning | Claude Sonnet | Extraction, drafting, intent routing, gap detection |
| Database | PostgreSQL + pgvector | Relational for events/profiles; pgvector for conversation memory |
| Message queue | Google Pub/Sub | Native Gmail push integration; fan-out across tenants |
| WhatsApp | Twilio or Meta Cloud API | Template management, webhook delivery |
| Voice transcription | Whisper API | WhatsApp voice note → text |
| Hosting | GCP Cloud Run | Serverless, auto-scales, Pub/Sub native |
| Scheduling | Cloud Scheduler → Pub/Sub | Daily digest, weekly summary, watch renewals |
| Auth | Google OAuth 2.0 | Per-caregiver, encrypted token storage |
| Email sending | SendGrid, Postmark, or AWS SES | Sends from Radar's domain, not caregiver's Gmail. Delivery monitoring, bounce handling. |
| Encryption | AES-256 + GCP Secret Manager | OAuth tokens at rest |
| Web UI | Minimal (OAuth redirect only) | Single-page, no framework needed |

---

## 17. Build Phases

### Phase 1 — Calendar + Conversational Input (4–6 weeks)

**Goal:** Validate that caregivers want to interact with a WhatsApp bot for family logistics.

**Scope:**
- WhatsApp group bot (basic Intent Router, Calendar Coordinator)
- Google Calendar integration (webhooks, read/write)
- Conversational onboarding
- Event creation from conversation ("Add soccer practice Tuesday at 4pm")
- Daily digest (when actionable) and weekly Sunday summary
- Basic conflict detection
- Forward-to email address (receive and store, basic extraction)

**Not included:** Gmail push, full extraction pipeline, logistics planner, research agent, seasons, gap detection.

### Phase 2 — Email Ingestion + Extraction (4–6 weeks)

**Goal:** Validate that automatic email → structured events saves meaningful time.

**Scope:**
- Gmail push integration (Pub/Sub)
- Email Extraction Agent (Haiku triage + Sonnet extraction)
- ActionItem extraction (forms, payments, items to bring — not just events)
- Event deduplication (email ↔ calendar)
- ICS feed subscription
- Extraction feedback loop (corrections logged)

### Phase 3 — Logistics Intelligence (4–6 weeks)

**Goal:** Differentiate from shared calendar apps. This is where Radar earns its name.

**Scope:**
- Logistics Planner Agent (prep checklists, gear tracking, transport coordination)
- Research Agent (gift suggestions, camp discovery)
- Recurring schedule detection and management
- Proactive gap detection
- Family profile learning (silent + weekly summary)
- Full suggest UX (all 5 interaction patterns)
- Assignment nudging

### Phase 4 — Growth Features (timeline TBD)

- Voice note transcription (Whisper API) — deferred from Phase 2; WhatsApp Business API doesn't provide transcripts natively
- Third-party integrations (RSVP submission, form filling, registration, payment) — until then, Radar reminds but users act manually on external services
- Playdate network effects (two-sided scheduling when both families use Radar)
- Crowdsourced carpooling
- Work calendar free/busy overlay
- SMS/iMessage ingestion
- Budget tracking for kid activities
- Broader home management (bills, maintenance, errands) — deferred, tracked separately
- Mobile companion app (optional, WhatsApp remains primary)

---

## 18. Decision Log

All major decisions made during the design process:

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Product type | Multi-tenant SaaS | Building for many families, not personal use |
| Interface | WhatsApp Business API | Where parents already are; group chat model fits naturally |
| Data sources (MVP) | Gmail push + GCal webhooks + forward-to email + ICS feeds | Real-time for connected accounts; forward path for privacy-cautious users |
| Autonomy model | Auto internal, suggest external | Internal actions are low-risk; external actions need human judgment |
| Family model | WhatsApp group = tenant, N caregivers, no roles | Neutral on family structure; group dynamics handle coordination naturally |
| Concurrent input | Consensus for SUGGEST, first-response for AUTO | External-facing actions must not execute on contradictory concurrent input; internal actions are low-stakes and reversible |
| Suggest UX | Open conversation state | More natural than buttons; LLM handles edit/approve classification |
| Onboarding | Conversational, 3 exchanges | Low friction; web UI only for OAuth |
| Family learning | Silent storage, weekly summary for correction | Keeps bot helpful without feeling intrusive |
| Recurring schedules | Generalized beyond sports (music, tutoring, swim, etc.). Confirm pattern once, then manage autonomously | One-time setup cost; ongoing benefit. "Season" was too sports-specific. |
| Email sending | Send from Radar's own domain, not caregiver Gmail | Gmail stays read-only. Eliminates risk of accidental sends/deletes from caregiver accounts. 10-second cancel window on approved sends. |
| Notifications | Daily when actionable + weekly always + immediate for urgent | High signal-to-noise ratio by default |
| OpenClaw | Architecture inspiration only, don't build on it | Single-user design is a dealbreaker for multi-tenant SaaS; security concerns |
| Work calendar | Deferred | Valuable but complex; free/busy API adds privacy concerns |
| SMS ingestion | Deferred to v2 | Programmatically hard; forward-to-WhatsApp workaround for now |
| Home management | Deferred | Focus on kids' activities first; same patterns apply later |
| Voice note transcription | Deferred to Phase 4 | WhatsApp Business API doesn't provide transcripts; not critical for email ingestion validation in Phase 2 |
| Third-party integrations | Deferred to Phase 4 | RSVPs, forms, registrations, payments — high complexity, each service is different. Phases 1–3 remind users of deadlines/actions; users act manually on external services. |
| Reinforcement learning | Prompt enrichment now, aggregate preference learning at scale, fine-tuning at 12–18 months | Per-family RL has cold-start problem; rich context in prompts achieves 80% of personalization value |
| GCal as source of truth | Schedule queries read from GCal API, not local DB | GCal includes manually-added events and external changes; local DB may be stale. Local DB is fallback only. |
| Email triage scope | All family member events, not just kids' activities | Adult events (dinner, concerts, travel) are equally important for family coordination |
| Timezone inference | Infer from event location, family timezone as fallback | Avoids incorrect UTC assumptions for cross-timezone events (e.g., Tempe AZ event at 7AM MST should not become 4AM ET) |
| Event update via conversation | Two-tier context: conversation history → GCal search | Enables natural follow-ups like "I already bought the gift" after confirming an event |
| WhatsApp 1:1 only (current) | Bot communicates 1:1 with each caregiver | WhatsApp Business API cannot be added to group chats; group chat support is a future consideration |

---

## 19. Deferred Features

Tracked for future consideration. Not in any current phase.

| Feature | Notes | Priority |
|---------|-------|----------|
| Work calendar free/busy overlay | Google FreeBusy API. Solves the #1 conflict detection gap. | High — Phase 4 candidate |
| SMS/iMessage ingestion | Requires device-level access or carrier integration. Workaround: forward to WhatsApp. | Medium |
| Broader home management | Bills, maintenance, errands. Same agent architecture applies. | Medium |
| Crowdsourced carpooling | Network effect feature. Requires critical mass of users in same area. | Low |
| Playdate network effects | Two-sided scheduling when both families use Radar. | Medium |
| Budget tracking | Track spending on activities, gifts, camps per child. | Low |
| Mobile companion app | Optional. WhatsApp remains primary interface. | Low |
| Kid-facing interface | Let older kids (13+) see their own schedule. Privacy implications. | Low |
| School/league partnerships | Direct integration with school admin systems, TeamSnap, etc. | Medium — depends on traction |
