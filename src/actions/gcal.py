"""Google Calendar actions: CRUD operations and watch channel management."""

import logging
import uuid
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.google_client import get_calendar_service, get_google_credentials
from src.config import settings
from src.state import families as families_dal
from src.state.models import Caregiver, Event

logger = logging.getLogger(__name__)

# GCal watch channels expire after 7 days; renew on 5-day intervals
WATCH_EXPIRY_DAYS = 7
WATCH_RENEW_INTERVAL_DAYS = 5


def _event_to_gcal_body(event: Event) -> dict:
    """Convert a Radar Event model to a Google Calendar API event body."""
    body: dict = {
        "summary": event.title,
        "start": {},
        "end": {},
    }

    if event.description:
        body["description"] = event.description
    if event.location:
        body["location"] = event.location

    # Use dateTime format (not date) for timed events
    # If the datetime has timezone info, use it; otherwise fall back to UTC
    start_dt = event.datetime_start
    body["start"]["dateTime"] = start_dt.isoformat()
    if start_dt.tzinfo is not None:
        # Let Google infer from the offset in the ISO string
        pass
    else:
        body["start"]["timeZone"] = "UTC"

    if event.datetime_end:
        body["end"]["dateTime"] = event.datetime_end.isoformat()
        if event.datetime_end.tzinfo is None:
            body["end"]["timeZone"] = "UTC"
    else:
        # Default to 1-hour duration
        end_dt = start_dt + timedelta(hours=1)
        body["end"]["dateTime"] = end_dt.isoformat()
        if start_dt.tzinfo is None:
            body["end"]["timeZone"] = "UTC"

    return body


async def list_upcoming_events(
    session: AsyncSession, family_id: UUID, days: int = 7
) -> list[dict]:
    """Query Google Calendar directly for upcoming events.

    Returns a list of simplified event dicts from GCal (the source of truth).
    Falls back to empty list if no caregiver has Google tokens.
    """
    caregivers = await families_dal.get_caregivers_for_family(session, family_id)

    for caregiver in caregivers:
        if caregiver.google_refresh_token_encrypted is None:
            continue

        try:
            credentials = await get_google_credentials(session, caregiver.id)
            service = get_calendar_service(credentials)

            now = datetime.now(UTC)
            time_max = now + timedelta(days=days)

            result = (
                service.events()
                .list(
                    calendarId="primary",
                    timeMin=now.isoformat(),
                    timeMax=time_max.isoformat(),
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=50,
                )
                .execute()
            )

            events = []
            for item in result.get("items", []):
                start = item.get("start", {})
                end = item.get("end", {})
                events.append({
                    "title": item.get("summary", "Untitled"),
                    "start": start.get("dateTime") or start.get("date", ""),
                    "end": end.get("dateTime") or end.get("date", ""),
                    "location": item.get("location"),
                    "description": item.get("description"),
                    "gcal_id": item.get("id"),
                })

            logger.info(
                "Fetched %d upcoming events from GCal for family %s",
                len(events),
                family_id,
            )
            return events

        except Exception:
            logger.exception(
                "Failed to fetch GCal events for caregiver %s", caregiver.id
            )
            continue

    logger.warning("No caregiver with Google tokens for family %s", family_id)
    return []


async def create_calendar_event(
    session: AsyncSession, family_id: UUID, event: Event
) -> list[str]:
    """Create an event on all connected caregivers' calendars.

    Returns a list of GCal event IDs (one per caregiver calendar).
    """
    caregivers = await families_dal.get_caregivers_for_family(session, family_id)
    gcal_event_ids: list[str] = []
    body = _event_to_gcal_body(event)

    for caregiver in caregivers:
        if caregiver.google_refresh_token_encrypted is None:
            logger.debug(
                "Skipping caregiver %s — no Google tokens", caregiver.id
            )
            continue

        try:
            credentials = await get_google_credentials(session, caregiver.id)
            service = get_calendar_service(credentials)
            result = (
                service.events()
                .insert(calendarId="primary", body=body)
                .execute()
            )
            gcal_event_id = result.get("id", "")
            gcal_event_ids.append(gcal_event_id)
            logger.info(
                "Created GCal event %s for caregiver %s",
                gcal_event_id,
                caregiver.id,
            )
        except Exception:
            logger.exception(
                "Failed to create GCal event for caregiver %s", caregiver.id
            )

    # Store GCal IDs as source_refs on the event
    if gcal_event_ids:
        existing_refs = event.source_refs or []
        event.source_refs = existing_refs + gcal_event_ids
        await session.flush()

    return gcal_event_ids


async def update_calendar_event(
    session: AsyncSession, family_id: UUID, event: Event
) -> None:
    """Update an event on all connected caregivers' calendars.

    Uses source_refs to find the GCal event IDs to update.
    """
    caregivers = await families_dal.get_caregivers_for_family(session, family_id)
    body = _event_to_gcal_body(event)
    gcal_ids = event.source_refs or []

    for caregiver in caregivers:
        if caregiver.google_refresh_token_encrypted is None:
            continue

        try:
            credentials = await get_google_credentials(session, caregiver.id)
            service = get_calendar_service(credentials)

            for gcal_id in gcal_ids:
                try:
                    service.events().update(
                        calendarId="primary",
                        eventId=gcal_id,
                        body=body,
                    ).execute()
                    logger.info(
                        "Updated GCal event %s for caregiver %s",
                        gcal_id,
                        caregiver.id,
                    )
                except Exception:
                    # Event may not exist on this caregiver's calendar
                    logger.debug(
                        "Could not update GCal event %s for caregiver %s (may not exist on their calendar)",
                        gcal_id,
                        caregiver.id,
                    )
        except Exception:
            logger.exception(
                "Failed to get credentials for caregiver %s", caregiver.id
            )


async def delete_calendar_event(
    session: AsyncSession, family_id: UUID, event: Event
) -> None:
    """Delete an event from all connected caregivers' calendars."""
    caregivers = await families_dal.get_caregivers_for_family(session, family_id)
    gcal_ids = event.source_refs or []

    for caregiver in caregivers:
        if caregiver.google_refresh_token_encrypted is None:
            continue

        try:
            credentials = await get_google_credentials(session, caregiver.id)
            service = get_calendar_service(credentials)

            for gcal_id in gcal_ids:
                try:
                    service.events().delete(
                        calendarId="primary",
                        eventId=gcal_id,
                    ).execute()
                    logger.info(
                        "Deleted GCal event %s for caregiver %s",
                        gcal_id,
                        caregiver.id,
                    )
                except Exception:
                    logger.debug(
                        "Could not delete GCal event %s for caregiver %s",
                        gcal_id,
                        caregiver.id,
                    )
        except Exception:
            logger.exception(
                "Failed to get credentials for caregiver %s", caregiver.id
            )


async def setup_gcal_watch(
    session: AsyncSession, caregiver: Caregiver
) -> None:
    """Create a push notification channel for a caregiver's primary calendar.

    Watch channels expire after 7 days. Renewal should happen on 5-day intervals.
    """
    if caregiver.google_refresh_token_encrypted is None:
        raise ValueError(
            f"Caregiver {caregiver.id} has no Google tokens"
        )

    credentials = await get_google_credentials(session, caregiver.id)
    service = get_calendar_service(credentials)

    channel_id = str(uuid.uuid4())
    expiration_ms = int(
        (datetime.now(UTC) + timedelta(days=WATCH_EXPIRY_DAYS)).timestamp()
        * 1000
    )

    # Build webhook URL for GCal push notifications
    # Requires a public URL — Google won't call localhost
    if not settings.webhook_base_url:
        raise ValueError(
            "WEBHOOK_BASE_URL must be set (e.g. your ngrok URL) for GCal push notifications"
        )
    webhook_url = settings.webhook_base_url.rstrip("/") + "/webhooks/gcal"

    watch_body = {
        "id": channel_id,
        "type": "web_hook",
        "address": webhook_url,
        "expiration": expiration_ms,
    }

    try:
        service.events().watch(
            calendarId="primary", body=watch_body
        ).execute()

        # Store channel info on caregiver
        caregiver.gcal_watch_channel_id = channel_id
        caregiver.gcal_watch_expiry = datetime.now(UTC) + timedelta(
            days=WATCH_EXPIRY_DAYS
        )
        # Perform initial sync to get a sync token
        sync_result = service.events().list(
            calendarId="primary",
            maxResults=1,
            singleEvents=True,
        ).execute()
        caregiver.gcal_sync_token = sync_result.get("nextSyncToken")
        await session.flush()

        logger.info(
            "Set up GCal watch for caregiver %s, channel=%s, expiry=%s",
            caregiver.id,
            channel_id,
            caregiver.gcal_watch_expiry,
        )
    except Exception:
        logger.exception(
            "Failed to set up GCal watch for caregiver %s", caregiver.id
        )
        raise


async def renew_gcal_watch(
    session: AsyncSession, caregiver: Caregiver
) -> None:
    """Renew an expiring GCal watch channel.

    Stops the old channel and creates a new one.
    """
    if caregiver.google_refresh_token_encrypted is None:
        raise ValueError(
            f"Caregiver {caregiver.id} has no Google tokens"
        )

    # Stop existing channel if present
    if caregiver.gcal_watch_channel_id:
        try:
            credentials = await get_google_credentials(session, caregiver.id)
            service = get_calendar_service(credentials)
            service.channels().stop(
                body={
                    "id": caregiver.gcal_watch_channel_id,
                    "resourceId": "primary",
                }
            ).execute()
            logger.info(
                "Stopped old GCal watch channel %s for caregiver %s",
                caregiver.gcal_watch_channel_id,
                caregiver.id,
            )
        except Exception:
            logger.warning(
                "Could not stop old GCal watch channel %s (may already be expired)",
                caregiver.gcal_watch_channel_id,
            )

    # Create new watch
    await setup_gcal_watch(session, caregiver)


async def fetch_calendar_changes(
    session: AsyncSession, caregiver: Caregiver
) -> list[dict]:
    """Fetch incremental calendar changes using the stored syncToken.

    Returns a list of raw GCal event dicts representing changes.
    Updates the caregiver's sync token for the next call.
    """
    if caregiver.google_refresh_token_encrypted is None:
        raise ValueError(
            f"Caregiver {caregiver.id} has no Google tokens"
        )

    credentials = await get_google_credentials(session, caregiver.id)
    service = get_calendar_service(credentials)

    changed_events: list[dict] = []
    page_token = None
    sync_token = caregiver.gcal_sync_token

    try:
        while True:
            kwargs: dict = {
                "calendarId": "primary",
                "singleEvents": True,
                "maxResults": 250,
            }

            if sync_token and not page_token:
                kwargs["syncToken"] = sync_token
            elif page_token:
                kwargs["pageToken"] = page_token
            else:
                # No sync token — do an initial full sync for recent events
                kwargs["timeMin"] = (
                    datetime.now(UTC) - timedelta(days=1)
                ).isoformat()

            result = service.events().list(**kwargs).execute()

            items = result.get("items", [])
            changed_events.extend(items)

            page_token = result.get("nextPageToken")
            if not page_token:
                # Save the new sync token
                new_sync_token = result.get("nextSyncToken")
                if new_sync_token:
                    caregiver.gcal_sync_token = new_sync_token
                    await session.flush()
                break

    except Exception as exc:
        # If sync token is invalid (410 Gone), reset and do full sync
        error_str = str(exc)
        if "410" in error_str or "Gone" in error_str:
            logger.warning(
                "Sync token expired for caregiver %s, performing full sync",
                caregiver.id,
            )
            caregiver.gcal_sync_token = None
            await session.flush()
            return await fetch_calendar_changes(session, caregiver)
        raise

    logger.info(
        "Fetched %d calendar changes for caregiver %s",
        len(changed_events),
        caregiver.id,
    )
    return changed_events
