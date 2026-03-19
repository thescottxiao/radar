# Radar — API Contract

> Webhook endpoints, OAuth routes, and internal API surface.

**Version:** 0.1.0
**Base URL:** `https://api.radar.app` (production) / `http://localhost:8000` (local)

---

## Webhook Endpoints

These endpoints receive inbound data from external services. All must return 200/204 quickly and queue processing asynchronously.

### POST `/webhooks/whatsapp`

Receives inbound WhatsApp messages from Twilio/Meta Cloud API.

**Headers:**
- `X-Twilio-Signature` or Meta webhook verification signature

**Payload (Twilio format):**
```json
{
  "From": "whatsapp:+15551234567",
  "To": "whatsapp:+15559876543",
  "Body": "What's on Saturday?",
  "NumMedia": "0",
  "MediaContentType0": "audio/ogg",
  "MediaUrl0": "https://...",
  "WaId": "15551234567",
  "ProfileName": "Sarah",
  "GroupId": "group_abc123"
}
```

**Behavior:**
1. Verify webhook signature.
2. Look up family by `GroupId` → `whatsapp_group_id`.
3. Look up caregiver by `From` phone number.
4. If `NumMedia > 0` and media is audio → queue for Whisper transcription → then process as text.
5. Queue message for Intent Router processing.
6. Return `200 OK` immediately.

**Response:** `200 OK` (empty body)

---

### GET `/webhooks/whatsapp/verify`

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
1. Decode Pub/Sub message.
2. Look up caregiver by `emailAddress`.
3. Fetch new messages since last `historyId` via Gmail API.
4. Queue each new message for Email Extraction Agent.
5. Update stored `historyId` for this caregiver.
6. Return `200 OK` immediately.

**Response:** `200 OK` (empty body). Must respond within 10 seconds or Pub/Sub retries.

---

### POST `/webhooks/gcal`

Receives Google Calendar push notifications.

**Headers:**
- `X-Goog-Channel-ID`: channel identifier (maps to caregiver)
- `X-Goog-Resource-ID`: calendar resource
- `X-Goog-Resource-State`: `sync` | `exists` | `not_exists`

**Behavior:**
1. Look up caregiver by channel ID.
2. If `Resource-State` is `sync` → initial sync, acknowledge and return.
3. Fetch changed events via Calendar API `events.list` with `syncToken`.
4. Queue each changed event for Calendar Change Detector.
5. Return `200 OK` immediately.

**Response:** `200 OK` (empty body)

---

### POST `/webhooks/forward-email`

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
1. Generate OAuth URL with scopes: `gmail.readonly`, `calendar`, `calendar.events`.
2. Store state parameter mapping to family_id + caregiver_phone.
3. Redirect to Google OAuth consent screen.

---

### GET `/auth/google/callback`

Handles Google OAuth redirect.

**Query params:**
- `code`: authorization code
- `state`: maps to family_id + caregiver_phone

**Behavior:**
1. Exchange code for tokens.
2. Encrypt refresh token (AES-256).
3. Store tokens in Caregiver record.
4. Set up Gmail watch channel (Pub/Sub) for this caregiver.
5. Set up GCal watch channels for all calendars.
6. Send WhatsApp confirmation to the family group: "[Caregiver name] connected ✓"
7. Redirect to a simple success page.

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
1. For each family, compile: week ahead, unconfirmed FamilyLearning entries, season updates, prep status.
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

Triggered by Cloud Scheduler every 30 minutes.

**Behavior:**
1. For each family with ICS feed subscriptions, fetch the feed.
2. Diff against stored events.
3. Queue new/changed events for Calendar Change Detector.

---

### POST `/internal/gaps/detect`

Triggered as part of the daily digest evaluation.

**Behavior:**
1. For each family, run gap detection rules (SPEC Section 12).
2. Generate suggestions for detected gaps.
3. Include in daily digest or send as immediate trigger if urgent.

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
