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
- **WhatsApp:** Twilio or Meta Cloud API
- **Voice:** Whisper API
- **Hosting:** GCP Cloud Run
- **Testing:** pytest + pytest-asyncio

## Code Organization

```
src/
├── ingestion/        # Webhook handlers, Pub/Sub consumers, ICS polling
│   ├── gmail.py      # Gmail push notification handler
│   ├── gcal.py       # GCal webhook handler
│   ├── whatsapp.py   # WhatsApp message handler (text + voice)
│   ├── forward.py    # Forward-to email inbound handler
│   └── ics.py        # ICS feed poller and differ
├── extraction/       # LLM-powered data extraction
│   ├── email.py      # Email Extraction Agent (Haiku triage + Sonnet extraction)
│   ├── calendar.py   # Calendar Change Detector (diff, dedup, season detection)
│   └── router.py     # Intent Router / Orchestrator (classifies WhatsApp messages)
├── agents/           # Reasoning agents
│   ├── calendar.py   # Calendar Coordinator (scheduling, conflicts, sync)
│   ├── logistics.py  # Logistics Planner (prep, gear, transport)
│   ├── research.py   # Research Agent (camps, gifts, venues)
│   └── reminders.py  # Reminder Engine (daily digest, weekly summary, immediate triggers)
├── actions/          # External side effects
│   ├── gcal.py       # Google Calendar write operations
│   ├── whatsapp.py   # WhatsApp message sending (templates + free-form)
│   ├── email.py      # Email sending (RSVPs, playdate messages)
│   └── state.py      # Family state updates (Event Registry, profiles, learnings)
├── state/            # Data access layer
│   ├── models.py     # SQLAlchemy models matching docs/schema.sql
│   ├── events.py     # Event Registry queries (CRUD, dedup, search)
│   ├── families.py   # Family and Caregiver queries
│   ├── children.py   # Child profile queries
│   ├── learning.py   # FamilyLearning queries
│   └── memory.py     # Conversation memory (short-term + pgvector retrieval)
├── auth/             # Authentication and tenant management
│   ├── oauth.py      # Google OAuth flow
│   ├── tokens.py     # Token encryption, storage, refresh
│   └── tenants.py    # Tenant lifecycle (create, onboard, delete)
└── api/              # FastAPI application
    ├── main.py       # App factory, middleware, startup
    ├── webhooks.py   # Webhook endpoints (Gmail, GCal, WhatsApp, forward-to)
    ├── oauth.py      # OAuth callback routes
    └── health.py     # Health check endpoints
```

## Development Rules

### Spec-Driven Development

1. **Read the SPEC before writing code.** Every feature maps to a section in `docs/SPEC.md`.
2. **If behavior isn't in the SPEC, don't implement it.** Ask first or propose a SPEC update.
3. **If you find a conflict between SPEC and code, flag it.** Don't silently resolve.
4. **Update the SPEC when behavior changes.** Code and SPEC stay in sync.

### Data Model Rules

1. **Every database query must filter by `family_id`.** No exceptions. This is the tenant isolation boundary.
2. **Never store raw email content.** Extract structured data, discard the raw email. Max 1-hour TTL in processing queues.
3. **OAuth tokens are always encrypted at rest** using AES-256. Never log tokens.
4. **Voice note audio files are deleted after transcription.** Never persisted.

### Agent Rules

1. **Email Extraction Agent uses two-tier model strategy:**
   - Haiku for triage (is this email relevant to family/kids?) — fast, cheap, ~80% rejection rate
   - Sonnet for full extraction (only on relevant emails) — structured output matching Event/ActionItem schemas
2. **Intent Router holds open conversation state** for pending approvals. Any reply to a pending SUGGEST action is classified as edit_instruction, approve, or dismiss. Not buttons.
3. **All SUGGEST-mode actions require caregiver approval** before execution. The bot never sends external emails, RSVPs, or purchases autonomously.
4. **AUTO actions execute without approval** but are always logged and surfaced in digests.
5. **Extraction confidence below 0.6** triggers explicit confirmation ("Is this right?"). Above 0.6 gets implicit correction opportunity.

### WhatsApp Rules

1. **Bot-initiated messages require approved templates.** Free-form only within 24-hour windows.
2. **Five template categories are pre-approved:** new_event, reminder, deadline_alert, approval_request, daily_digest, weekly_summary, assignment_nudge, conflict_alert.
3. **Voice notes** are transcribed via Whisper API before entering the intent pipeline.
4. **First response wins** when multiple caregivers reply simultaneously.

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

**Current phase: Phase 1**

Only implement features scoped to the current phase unless explicitly told otherwise.

## Common Pitfalls

- **GCal/Gmail watch channels expire every 7 days.** Auto-renewal on 5-day intervals. Always check expiry before assuming a watch is active.
- **WhatsApp 24-hour window.** Can't send free-form messages after window closes. Design flows to open with a template.
- **Dedup is fuzzy, not exact.** datetime ±30 min AND title similarity > 0.7. Edge cases will exist — prefer false negatives (miss a dup) over false positives (incorrectly merge distinct events).
- **Season exceptions don't modify the season pattern.** Only the individual instance is changed.
- **Family learning entries start unconfirmed.** They become confirmed after being surfaced in a weekly summary with no correction.
