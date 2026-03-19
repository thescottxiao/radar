# Radar

A WhatsApp-native AI assistant that helps busy parents coordinate their kids' activities — from scheduling and calendar sync to event prep, transportation logistics, and proactive reminders.

Radar reads your email and calendar, surfaces what matters, and helps caregivers stay coordinated without the stress.

## How it works

Radar lives in a WhatsApp group with the family's caregivers. It connects to each caregiver's Gmail and Google Calendar to automatically detect events, deadlines, and action items from incoming emails and calendar changes.

- **Detects events** from school emails, party invites, sports league announcements, and camp registrations
- **Syncs calendars** across all caregivers and flags scheduling conflicts (including work calendar availability)
- **Prepares for events** with auto-generated checklists — gifts, gear, forms, RSVPs
- **Coordinates transportation** by surfacing who's available and assigning pickup/dropoff
- **Sends smart reminders** — daily when there's something actionable, weekly summary every Sunday
- **Learns your family** silently over time, surfacing what it learned for correction

## Architecture

See [docs/architecture.html](docs/architecture.html) for the full system design.

**Four-layer agent architecture:**

```
Ingestion    →  Gmail Push · GCal Webhooks · WhatsApp · ICS Feeds · Forward-to Email
Extraction   →  Email Extraction Agent · Calendar Change Detector · Intent Router
Reasoning    →  Calendar Coordinator · Logistics Planner · Research Agent · Reminder Engine
Action       →  Auto (calendar writes, reminders) · Suggest (RSVPs, purchases, external messages)
```

## Tech stack

- **Runtime:** Python + FastAPI (async)
- **LLM:** Claude Haiku (triage) + Sonnet (reasoning/extraction)
- **Database:** PostgreSQL + pgvector
- **Messaging:** WhatsApp Business API (Twilio or Meta Cloud API)
- **Ingestion:** Google Pub/Sub (Gmail), GCal webhooks, ICS feed polling
- **Hosting:** GCP Cloud Run
- **Voice:** Whisper API for voice note transcription

## Project structure

```
radar/
├── src/
│   ├── ingestion/        # Gmail push, GCal webhooks, WhatsApp handler, ICS feeds
│   ├── extraction/       # Email parser, calendar diff, intent router
│   ├── agents/           # Calendar coordinator, logistics planner, research, reminders
│   ├── actions/          # GCal writer, WhatsApp notifier, email drafter, RSVP handler
│   ├── state/            # Event registry, family profiles, conversation memory
│   ├── auth/             # Google OAuth, tenant management, token encryption
│   └── api/              # FastAPI routes, webhook endpoints
├── docs/                 # Architecture docs, research
├── tests/
├── scripts/
└── docker-compose.yml
```

## Setup

```bash
cp .env.example .env
# Fill in your API keys and credentials
```

## License

TBD
