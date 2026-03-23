# Radar — API Contract

> Webhook endpoints, OAuth routes, and internal API surface.

**Version:** 0.2.0
**Base URL:** `https://api.radar.app` (production) / `http://localhost:8000` (local)

---

## Webhook Endpoints

These endpoints receive inbound data from external services. All must return 200/204 quickly and queue processing asynchronously.

### POST `/webhooks/whatsapp`

Receives inbound WhatsApp messages from Meta Cloud API.

**Headers:**
- `X-Hub-Signature-256`: Meta webhook verification signature

**Payload (text message):**
```json
{
  "entry": [{
    "changes": [{
      "value": {
        "contacts": [{"wa_id": "15551234567", "profile": {"name": "Sarah"}}],
        "messages": [{
          "from": "15551234567",
          "type": "text",
          "text": {"body": "What's on Saturday?"}
        }]
      }
    }]
  }]
}
```

**Payload (interactive button reply):**
```json
{
  "entry": [{
    "changes": [{
      "value": {
        "messages": [{
          "from": "15551234567",
          "type": "interactive",
          "interactive": {
            "type": "button_reply",
            "button_reply": {
              "id": "event_confirm:{pending_action_id}:yes",
              "title": "Yes"
            }
          }
        }]
      }
    }]
  }]
}
```

**Behavior:**
1. Verify webhook signature via `X-Hub-Signature-256` (skip if `WHATSAPP_WEBHOOK_SECRET` is unset for local dev).
2. Extract sender phone from payload and look up caregiver.
3. If message type is `interactive` with `button_reply` → extract `button_reply.id` and pass to Intent Router for direct routing (no LLM classification, confidence=1.0).
4. If message type is `document` with `.ics` extension → process as ICS attachment.
5. Queue message for Intent Router processing.
6. Return `200 OK` immediately.

**Response:** `{"status": "ok"}`

---

### GET `/webhooks/whatsapp`

WhatsApp webhook verification (Meta Cloud API setup).

**Query params:**
- `hub.mode`: `subscribe`
- `hub.verify_token`: must match `WHATSAPP_VERIFY_TOKEN`
- `hub.challenge`: echo back on success

**Response:** `200` with `hub.challenge` value, or `403` on token mismatch.

---

### POST `/webhooks/gmail`

Receives Gmail push notifications via Google Pub/Sub.

**Payload:**
```json
{
  "message": {
    "data": "<base64-encoded>",
    "messageId": "123",
    "publishTime": "2026-03-19T10:00:00Z"
  },
  "subscription": "projects/radar/subscriptions/gmail-push"
}
```

**Decoded data:**
```json
{
  "emailAddress": "sarah@gmail.com",
  "historyId": 12345
}
```

**Behavior:**
1. Decode Pub/Sub message (base64 → JSON with `emailAddress` and `historyId`).
2. Look up caregiver by `emailAddress`.
3. Refresh access token using stored encrypted refresh token.
4. Fetch new message IDs since last `historyId` via Gmail History API (filters: `messageAdded`, `INBOX` label, excludes `SPAM`/`TRASH`).
5. For each new message:
   a. Fetch full message content via Gmail API.
   b. Run through Email Extraction Agent (Haiku triage → Sonnet extraction).
   c. Auto-persist action items and learnings (AUTO mode).
   d. For each extracted event: create `PendingAction` (type: `event_confirmation`) and send WhatsApp interactive button message (Yes/No) to all caregivers in the family.
6. Update stored `historyId` for this caregiver.
7. Return `200 OK` immediately.

**Response:** `200 OK` (empty body). Must respond within 10 seconds or Pub/Sub retries.

---

### POST `/webhooks/gcal`

Receives Google Calendar push notifications.

**Headers:**
- `X-Goog-Channel-ID`: channel identifier (maps to caregiver)
- `X-Goog-Resource-ID`: calendar resource
- `X-Goog-Resource-State`: `sync` | `exists` | `not_exists`

**Behavior:**
1. Look up caregiver by `X-Goog-Channel-ID` → `gcal_watch_channel_id`.
2. If `Resource-State` is `sync` → initial sync handshake, acknowledge and return.
3. Fetch changed events via Calendar API `events.list` with stored `syncToken` (incremental sync). On 410 Gone, reset sync token and do full sync.
4. For each changed event: notify all caregivers in the family via WhatsApp with event summary.
5. Update stored `syncToken` for the next incremental sync.
6. Return `200 OK` immediately.

**Response:** `200 OK` (empty body)

---

### POST `/webhooks/forward-email`

> **Status: Planned — route not yet wired.** The handler logic exists at `src/ingestion/forward.py` but no API route is registered in `src/api/webhooks.py`.

Receives forwarded emails at `family-{id}@radar.app`. Handled by an inbound email service (SendGrid Inbound Parse, Mailgun, or AWS SES).

**Payload (SendGrid format):**
```json
{
  "from": "sarah@gmail.com",
  "to": "family-abc123@radar.app",
  "subject": "Fwd: Soccer practice schedule",
  "text": "...",
  "html": "...",
  "attachments": "..."
}
```

**Behavior:**
1. Extract `family_id` from the `to` address.
2. Verify `from` address belongs to a caregiver in that family (or accept anyway with lower confidence).
3. Queue email content for Email Extraction Agent (same pipeline as Gmail push).
4. Return `200 OK`.

**Response:** `200 OK`

---

## OAuth Routes

### GET `/auth/google`

Initiates Google OAuth flow for a caregiver.

**Query params:**
- `family_id`: uuid
- `caregiver_phone`: WhatsApp phone number (for linking)

**Behavior:**
1. Generate OAuth URL with scopes: `gmail.readonly`, `calendar.events`. Note: Gmail is read-only. Radar never has send/modify/delete access to caregiver email. External emails are sent from Radar's own domain.
2. Store state parameter mapping to family_id + caregiver_phone.
3. Redirect to Google OAuth consent screen.

---

### GET `/auth/google/callback`

Handles Google OAuth redirect.

**Query params:**
- `code`: authorization code
- `state`: maps to family_id + caregiver_phone

**Behavior:**
1. Exchange code for tokens (PKCE code_verifier flow).
2. Encrypt refresh token (AES-256).
3. Store tokens and Google account email in Caregiver record.
4. Set up GCal push notification watch channel for the caregiver's primary calendar (requires `WEBHOOK_BASE_URL`). Stores channel ID and expiry on the caregiver record.
5. Set up Gmail Pub/Sub watch on the caregiver's inbox (requires `GMAIL_PUBSUB_TOPIC`). Stores historyId and expiry on the caregiver record.
6. Send WhatsApp confirmation to the caregiver: "[Name] connected Google account successfully. Calendar and email sync is now active."
7. Redirect to a success HTML page (can be closed; return to WhatsApp).

---

## Email Sending (Internal)

Radar sends external emails from its own domain, not from caregiver Gmail accounts.

### POST `/internal/email/send`

> **Status: Planned — not yet implemented.**

Triggered when a caregiver approves an external email (RSVP, playdate message, coach email).

**Payload:**
```json
{
  "family_id": "uuid",
  "pending_action_id": "uuid",
  "approved_by": "uuid (caregiver)",
  "from_display_name": "Sarah",
  "to": "sophia.mom@gmail.com",
  "subject": "Re: Sophia's Birthday Party",
  "body": "Hi! Emma would love to come..."
}
```

**Behavior:**
1. Wait 10 seconds (cancel window). If caregiver sends "cancel" during this window, abort.
2. Send email via SendGrid/Postmark/SES from `{name}@notifications.radar.app`.
3. Log in `sent_emails` table with full content, recipient, approving caregiver, and edit history.
4. Update pending action status to `approved`.
5. Send WhatsApp confirmation: "Sent ✓"
6. Monitor delivery status via webhook from email provider.

**From address format:** `sarah-via-radar@notifications.radar.app` with display name "Sarah via Radar"

---

## Internal API (not externally exposed)

These are internal service routes used by the scheduler and background workers.

### POST `/internal/digest/daily`

Triggered by Cloud Scheduler every morning at the family's configured `daily_digest_time`.

**Behavior:**
1. For each family, evaluate Event Registry and ActionItem list.
2. If there's actionable content → generate digest via LLM → send WhatsApp template.
3. If nothing actionable → skip (do NOT send "nothing today").

---

### POST `/internal/digest/weekly`

Triggered by Cloud Scheduler on the family's configured `weekly_summary_day` at `weekly_summary_time`.

**Behavior:**
1. For each family, compile: week ahead, unconfirmed FamilyLearning entries, recurring schedule updates, prep status.
2. Generate summary via LLM.
3. Send WhatsApp template. Always sends, regardless of content.
4. Mark surfaced FamilyLearning entries as `surfaced_in_summary = true`.

---

### POST `/internal/watches/renew`

Triggered by Cloud Scheduler every 24 hours.

**Behavior:**
1. Query all caregivers where `gmail_watch_expiry` or `gcal_watch_expiry` is within 48 hours.
2. Renew watch channels via Gmail/GCal APIs.
3. Update expiry timestamps.
4. If renewal fails → notify caregiver in WhatsApp group with re-auth link.

---

### POST `/internal/ics/poll`

> **Status: Planned — not yet implemented.** The ICS polling logic exists at `src/ingestion/ics.py` but no scheduler route is registered.

Triggered by Cloud Scheduler every 30 minutes.

**Behavior:**
1. For each family with ICS feed subscriptions, fetch the feed.
2. Diff against stored events.
3. Queue new/changed events for Calendar Change Detector.

---

### POST `/internal/gaps/detect`

> **Status: Planned — not yet implemented (Phase 3).**

Triggered as part of the daily digest evaluation.

**Behavior:**
1. For each family, run gap detection rules (SPEC Section 12).
2. Generate suggestions for detected gaps.
3. Include in daily digest or send as immediate trigger if urgent.

### POST `/internal/test/simulate-email`

**DEV ONLY.** Simulates an incoming email through the full extraction pipeline without requiring Gmail integration.

**Payload:**
```json
{
  "family_id": "uuid (defaults to test family)",
  "from_address": "coach@example.com",
  "subject": "Soccer Tournament This Saturday",
  "body": "..."
}
```

**Behavior:**
1. Run email through two-tier extraction (Haiku triage → Sonnet extraction).
2. Auto-persist action items and learnings.
3. For each extracted event: create `PendingAction` and send WhatsApp buttons.
4. If only action items (no events): send plain text WhatsApp summary.

**Response:**
```json
{
  "status": "processed",
  "is_relevant": true,
  "events_pending_confirmation": 1,
  "action_items": 2,
  "learnings": 0,
  "summary": "..."
}
```

---

### POST `/internal/reconcile`

Trigger GCal reconciliation for all families with connected Google accounts.

**Behavior:**
1. For each family with Google tokens, compare local DB events (next 30 days) against GCal.
2. Import new GCal events, soft-delete locally if removed from GCal, push local corrections to GCal via outbox.
3. Conflict resolution: `source=calendar` events → GCal wins; all other sources → local DB wins.

**Response:**
```json
{
  "status": "complete",
  "families": 5,
  "created": 2,
  "updated": 1,
  "cancelled": 0,
  "pushed": 3,
  "skipped": 0
}
```

### POST `/internal/reconcile/{family_id}`

Trigger GCal reconciliation for a single family. Same behavior as above, scoped to one family.

---

## Health & Monitoring

### GET `/health`

Returns service health status.

**Response:**
```json
{
  "status": "healthy",
  "version": "0.1.0",
  "database": "connected",
  "pubsub": "connected"
}
```

### GET `/health/ready`

Readiness check for Cloud Run. Returns 200 when the service is ready to accept traffic.

---

## Rate Limits & Timeouts

- All webhook endpoints must respond within **10 seconds** (Pub/Sub and WhatsApp retry on timeout).
- Processing is always async — webhooks acknowledge receipt and queue work.
- Gmail API: respect per-user rate limits (250 quota units/second per user).
- WhatsApp: max 80 messages/second for the business account (shared across all families).
- GCal API: 1,000,000 queries/day for the project.

## Error Handling

- Webhook endpoints always return `200 OK` even if processing fails (to prevent retry storms).
- Processing failures are logged and retried via the message queue with exponential backoff.
- If a caregiver's OAuth token fails refresh 3 times, the bot notifies them in the WhatsApp group and stops processing their email/calendar until re-authed.
