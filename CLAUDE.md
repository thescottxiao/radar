# CLAUDE.md — Instructions for AI Coding Agents

This file provides context and rules for AI agents (Claude Code, Cursor, etc.) working on the Radar codebase.

## Project Summary

Radar is a WhatsApp-native AI assistant that helps busy parents coordinate their kids' activities. It connects to Gmail and Google Calendar, extracts events and action items from emails, syncs calendars across caregivers, and proactively manages logistics like transportation, event prep, and reminders.

## Key Documents

Read these before making changes:

- **`docs/SPEC.md`** — The source of truth. All product behavior, data models, agent specs, and interaction patterns are defined here. If the SPEC says one thing and the code says another, the SPEC wins (or the SPEC needs updating first).
- **`docs/architecture.html`** — Visual reference for system design. Helpful for understanding how layers interact.
- **`docs/API.md`** — Webhook endpoints and route contracts.
- **`docs/schema.sql`** — Database schema. Migrations should be generated from changes here.

## Architecture Overview

Four-layer agent system:

```
Layer 0: Ingestion    — Gmail Push (Pub/Sub), GCal Webhooks, WhatsApp Business API,
                        Forward-to Email, ICS Feed polling
Layer 1: Extraction   — Email Extraction Agent, Calendar Change Detector, Intent Router
Layer 2: Reasoning    — Calendar Coordinator, Logistics Planner, Research Agent, Reminder Engine
Layer 3: Action       — AUTO (calendar writes, notifications, state updates)
                        SUGGEST (email drafts, RSVPs, purchases — require caregiver approval)
```

Shared state: PostgreSQL with row-level tenant isolation (`family_id` on every table).

## Tech Stack

- **Language:** Python 3.11+
- **Framework:** FastAPI (async)
- **Database:** PostgreSQL + pgvector
- **LLM:** Claude API (Haiku for triage, Sonnet for reasoning/extraction)
- **Message queue:** Google Pub/Sub
- **WhatsApp:** Meta Cloud API
- **Voice:** Whisper API
- **Hosting:** GCP Cloud Run
- **Testing:** pytest + pytest-asyncio

## Code Organization

```
src/
├── config.py         # Environment/settings configuration
├── db.py             # SQLAlchemy async session factory, tenant isolation
├── llm.py            # LLM client wrapper (Haiku/Sonnet, tool_use extraction)
├── whatsapp_client.py # Meta Cloud API client (send, verify, templates)
├── ingestion/        # Webhook handlers, Pub/Sub consumers, ICS polling
│   ├── gmail.py      # Gmail push notification handler
│   ├── gcal.py       # GCal webhook handler
│   ├── whatsapp.py   # WhatsApp message handler (text + buttons + documents)
│   ├── forward.py    # Forward-to email inbound handler
│   ├── ics.py        # ICS feed poller, attachment processor, calendar parser
│   └── schemas.py    # Ingestion data schemas (EmailContent, EmailAttachment)
├── extraction/       # LLM-powered data extraction
│   ├── email.py      # Email Extraction Agent (Haiku triage + Sonnet extraction)
│   ├── calendar.py   # Calendar Change Detector (diff, dedup, import)
│   ├── router.py     # Intent Router / Orchestrator (classifies WhatsApp messages)
│   ├── dedup.py      # Fuzzy event deduplication (±30 min + title similarity)
│   └── schemas.py    # Extraction data schemas (IntentType, ExtractedEvent, etc.)
├── agents/           # Reasoning agents
│   ├── calendar.py   # Calendar Coordinator (scheduling, conflicts, transport)
│   ├── reminders.py  # Reminder Engine (daily digest, weekly summary)
│   ├── onboarding.py # Conversational 3-step family onboarding
│   ├── context.py    # Family context builder for LLM prompts
│   ├── recurrence_detector.py  # Recurring schedule pattern detection
│   └── schemas.py    # Agent data schemas (ResolvedEvent, Conflict, etc.)
├── actions/          # External side effects
│   ├── gcal.py       # Google Calendar CRUD operations
│   ├── gcal_outbox_processor.py  # Background loop processing GCal outbox items
│   ├── gcal_reconciler.py        # Periodic local DB ↔ GCal reconciliation
│   ├── whatsapp.py   # WhatsApp message sending (templates + free-form)
│   └── state.py      # Family state updates (Event Registry, profiles, learnings)
├── state/            # Data access layer
│   ├── models.py     # SQLAlchemy models matching docs/schema.sql
│   ├── events.py     # Event Registry queries (CRUD, dedup, search)
│   ├── outbox.py     # GCal outbox DAL (enqueue, claim, mark done/failed)
│   ├── families.py   # Family and Caregiver queries
│   ├── children.py   # Child profile queries
│   ├── learning.py   # FamilyLearning queries
│   ├── memory.py     # Conversation memory (short-term + pgvector retrieval)
│   ├── schedules.py  # RecurringSchedule queries
│   ├── preferences.py # CaregiverPreferences queries
│   ├── pending.py    # PendingAction CRUD and expiration
│   └── feedback.py   # ExtractionFeedback for training data collection
├── auth/             # Authentication and tenant management
│   ├── oauth.py      # Google OAuth flow
│   ├── tokens.py     # Token encryption, storage, refresh
│   ├── google_client.py # Google API service factory with token refresh
│   └── tenants.py    # Tenant lifecycle (create, onboard, delete)
├── api/              # FastAPI application
│   ├── main.py       # App factory, middleware, startup
│   ├── webhooks.py   # Webhook endpoints (Gmail, GCal, WhatsApp)
│   ├── oauth.py      # OAuth callback routes
│   ├── internal.py   # Cloud Scheduler-triggered internal routes
│   └── health.py     # Health check endpoints
└── utils/            # Shared utilities
    ├── button_ids.py # Button ID encoding/decoding for WhatsApp routing
    ├── timezone.py   # Family timezone utilities
    └── rrule.py      # RRULE to GCal recurrence conversion
```

## Development Rules

### Spec-Driven Development

1. **Read the SPEC before writing code.** Every feature maps to a section in `docs/SPEC.md`.
2. **If behavior isn't in the SPEC, don't implement it.** Ask first or propose a SPEC update.
3. **If you find a conflict between SPEC and code, flag it.** Don't silently resolve.
4. **Update the SPEC when behavior changes.** Code and SPEC stay in sync.
5. **Always update docs after making behavioral changes.** When you change how a feature works, update `docs/SPEC.md`, `docs/API.md`, and/or `CLAUDE.md` as appropriate. Docs and code must never drift apart.

### Data Model Rules

1. **Every database query must filter by `family_id`.** No exceptions. This is the tenant isolation boundary.
2. **Never store raw email content.** Extract structured data, discard the raw email. Max 1-hour TTL in processing queues.
3. **OAuth tokens are always encrypted at rest** using AES-256. Never log tokens.
4. **Gmail scope is read-only.** Radar never sends from, modifies, or deletes caregiver emails. External emails are sent from Radar's own domain via SendGrid/Postmark/SES.
5. **Treat email content as untrusted data** in LLM prompts. Never execute email content as agent instructions (prompt injection defense).
4. **Voice note audio files are deleted after transcription.** Never persisted.
6. **Event type is a free-form text field**, not a DB enum. The LLM can return any descriptive string (e.g., "birthday party", "swim meet", "reception"). No validation or normalization needed.

### Agent Rules

1. **Email Extraction Agent uses two-tier model strategy:**
   - Haiku for triage (is this email relevant to any family member's activities, events, or scheduling — including adult/parent events?) — fast, cheap, ~80% rejection rate. Triage result is parsed with `startswith("RELEVANT")` to handle LLM explanations.
   - Sonnet for full extraction (only on relevant emails) — structured output matching Event/ActionItem schemas. Extractions include detailed descriptions with prep checklists (☐ format) and timezone-aware datetimes (inferred from location, family timezone as fallback).
2. **Intent Router uses conversation context** (last 10 messages) for classification. Supports `event_update` intent for follow-up messages about recently confirmed events (e.g., "I already bought the wedding gift"). Uses two-tier matching: recent conversation → GCal search. Pending approvals are checked first — any reply to a pending SUGGEST action is classified as edit_instruction, approve, or dismiss.
3. **All SUGGEST-mode actions require caregiver approval** before execution. The bot never sends external emails, RSVPs, or purchases autonomously. External emails are sent from Radar's own domain (not the caregiver's Gmail). A 10-second cancel window is shown after approval.
4. **AUTO actions execute without approval** but are always logged and surfaced in digests.
5. **Concurrent input on SUGGEST actions requires consensus.** If multiple caregivers respond with contradictory instructions to a pending external action, the bot pauses and surfaces the conflict. It does not execute until resolved.
5. **Extraction confidence below 0.6** triggers explicit confirmation ("Is this right?"). Above 0.6 gets implicit correction opportunity.
6. **Local DB is authoritative for schedule queries.** `_handle_query_schedule` queries `events_dal.get_upcoming_events()` first. Falls back to GCal API only if local DB has no events for the family (e.g., pre-sync).
7. **Confirmed events are written to local DB first, then synced to GCal via the outbox.** When a caregiver confirms an event, it's created in the Event Registry and a `create` entry is enqueued in `gcal_outbox`. The outbox processor handles GCal API calls asynchronously with retry.
8. **Event updates go through the outbox.** When a user updates an event (e.g., "I bought the gift"), the local DB is updated first, then a `patch` or `update` entry is enqueued in `gcal_outbox`.
9. **GCal webhook changes are imported into the authoritative local DB.** Changes made directly in GCal are synced to the Event Registry silently (no WhatsApp notifications). For events with `source=calendar`, GCal wins on reconciliation. For all other sources, local DB wins.
10. **GCal writes go through the outbox.** Never call GCal API directly from request handlers. Use `outbox_dal.enqueue_gcal_write()` instead. The outbox processor (`gcal_outbox_processor.py`) polls every 5s with exponential backoff retry (30s → 2h, max 5 retries).
11. **Outbox idempotency keys prevent duplicate GCal operations.** Format: `create:{event_id}` for creates, `update:{event_id}:{timestamp_ms}` or `patch:{gcal_id}:{timestamp_ms}` for updates.
12. **Cancel/modify handlers use smart event matching.** `_handle_cancel_event` and `_handle_modify_event` use a two-tier context strategy (conversation memory + local DB search with 90-day window) and LLM matching to identify events, same as `_handle_event_update`.
13. **Transport coordination only applies to child events with 2+ caregivers.** Gate on: `family.children` non-empty AND `event.child_id IS NOT NULL` AND `len(active_caregivers) >= 2`. Single-caregiver families get silent auto-assignment. Families with no children skip transport entirely.
14. **Transport routines are inferred, not asked.** After 3 consistent claims by the same caregiver for the same (recurring_schedule, day_of_week, role), create an unconfirmed FamilyLearning. Confirmed via weekly summary with no correction. Never ask "who usually handles pickup?" upfront.
15. **Sibling transport conflicts are flagged, not resolved.** When the same caregiver is assigned to overlapping events (±30 min) for different children at different locations, notify all caregivers. Do not propose which caregiver should swap.
16. **Transport swaps clear the instance, not the routine.** "I can't do pickup Thursday" clears that event's assignment but leaves the RecurringSchedule default intact for future weeks.

### WhatsApp Rules

1. **Bot-initiated messages require approved templates.** Free-form only within 24-hour windows.
2. **Five template categories are pre-approved:** new_event, reminder, deadline_alert, approval_request, daily_digest, weekly_summary, assignment_nudge, conflict_alert.
3. **Voice notes** — transcription deferred to Phase 4. Voice messages are not supported until then.
4. **First response wins** when multiple caregivers reply simultaneously.
5. **WhatsApp Business API is 1:1 only.** The bot cannot be added to group chats (current platform limitation). All messages are sent to individual caregivers. Notifications go to all caregivers in the family individually.

### Testing

1. Write tests for every agent's core logic.
2. Use fixtures for family/caregiver/event test data.
3. Mock LLM calls in unit tests — don't hit the API.
4. Integration tests should verify tenant isolation (queries never leak cross-family).
5. Test dedup logic explicitly — it's the most error-prone extraction behavior.

### Git Conventions

- Commit messages: imperative mood, concise first line, detail in body if needed.
- Branch naming: `feature/short-description`, `fix/short-description`
- Always reference SPEC section in PR descriptions when implementing features.

## Build Phases

We are building in phases. Check which phase is current before working on features:

- **Phase 1:** Calendar + Conversational Input (WhatsApp bot, GCal integration, basic scheduling, daily/weekly digests)
- **Phase 2:** Email Ingestion + Extraction (Gmail push, Email Extraction Agent, ActionItems, dedup, ICS feeds, voice notes)
- **Phase 3:** Logistics Intelligence (Logistics Planner, Research Agent, seasons, gap detection, full suggest UX)
- **Phase 4:** Growth features (playdate networks, carpooling, work calendar overlay)

**Current phase: Phase 1 + Phase 2 (implemented)**

Phase 1 and Phase 2 code is implemented. Phase 3+ features should not be built unless explicitly told otherwise.

## Common Pitfalls

- **GCal/Gmail watch channels expire every 7 days.** Auto-renewal on 5-day intervals. Always check expiry before assuming a watch is active.
- **WhatsApp 24-hour window.** Can't send free-form messages after window closes. Design flows to open with a template.
- **Dedup is fuzzy, not exact.** datetime ±30 min AND title similarity > 0.7. Edge cases will exist — prefer false negatives (miss a dup) over false positives (incorrectly merge distinct events).
- **Recurring schedule exceptions don't modify the overall pattern.** Only the individual instance is changed. "Recurring schedule" is the generalized term (not "season") — covers sports, music lessons, tutoring, swim, etc.
- **Family learning entries start unconfirmed.** They become confirmed after being surfaced in a weekly summary with no correction.
- **Every sent email must be logged** in the sent_emails audit table with full content, recipient, approving caregiver, and edit history.
- **Email triage parsing is lenient.** Use `result.startswith("RELEVANT")`, not exact match — the LLM may append explanation text after the keyword.
- **Timezone handling.** Never hardcode UTC. Extraction prompts instruct the LLM to infer timezone from event location. `_event_to_gcal_body()` passes timezone info from the datetime object to GCal. If no tzinfo, falls back to UTC.
- **OAuth callback sets up both GCal and Gmail watches.** Both are in try/except blocks — if `WEBHOOK_BASE_URL` or `GMAIL_PUBSUB_TOPIC` aren't configured, the watches silently fail and can be retried later.
- **GCal watch channels can be stale.** After re-OAuth, old channels may still send notifications with unknown channel IDs. These are logged as warnings and ignored. They expire naturally after 7 days.
- **Meta test mode.** During development, phone numbers must be added to the Meta Developer Dashboard allowed list. WhatsApp sends to non-allowed numbers fail with "Recipient phone number not in allowed list."
- **Gmail caregiver lookup must be unique.** Only one caregiver per family should have a given `google_account_email` to avoid `MultipleResultsFound` errors.
