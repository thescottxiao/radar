"""Google Calendar actions: CRUD operations and watch channel management."""

import logging
import uuid
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.google_client import get_calendar_service, get_google_credentials
from src.config import settings
from src.state import families as families_dal
from src.state.models import Caregiver, Event


def _safe_get_relationship(event: Event, attr: str) -> list:
    """Access a SQLAlchemy relationship without triggering lazy load.

    Returns the loaded collection if available, or an empty list if
    the relationship hasn't been eagerly loaded.
    """
    state = sa_inspect(event, raiseerr=False)
    if state is None:
        # Not a SQLAlchemy instance (e.g., a mock or plain object)
        return getattr(event, attr, []) or []
    if attr not in state.dict:
        return []
    return getattr(event, attr, []) or []

logger = logging.getLogger(__name__)


def _strip_gcal_refs(refs: list[str]) -> list[str]:
    """Strip the 'gcal:' prefix from source_refs to get raw GCal event IDs."""
    return [r.removeprefix("gcal:") for r in refs if r.startswith("gcal:")]


# GCal watch channels expire after 7 days; renew on 5-day intervals
WATCH_EXPIRY_DAYS = 7
WATCH_RENEW_INTERVAL_DAYS = 5


def event_to_gcal_body(
    event: Event,
    caregiver_map: dict[UUID, str] | None = None,
    child_map: dict[UUID, str] | None = None,
) -> dict:
    """Convert a Radar Event model to a Google Calendar API event body.

    Args:
        event: The Radar Event model.
        caregiver_map: Optional mapping of caregiver UUID → display name,
            used to append transport and attendee sections to the description.
        child_map: Optional mapping of child UUID → display name,
            used to show child attendees in the description.
    """
    body: dict = {
        "summary": event.title,
        "start": {},
        "end": {},
    }

    description = event.description or ""

    # Append attendee section if attendees are linked
    attendee_names = []
    if child_map:
        for ec in _safe_get_relationship(event, "children"):
            name = child_map.get(ec.child_id)
            if name:
                attendee_names.append(name)
    if caregiver_map:
        for ec in _safe_get_relationship(event, "caregivers"):
            name = caregiver_map.get(ec.caregiver_id)
            if name:
                attendee_names.append(name)
    if attendee_names:
        attendee_marker = "\n\nFor: "
        # Strip existing attendee section before re-appending
        if attendee_marker in description:
            description = description[: description.index(attendee_marker)]
        description += f"\n\nFor: {', '.join(attendee_names)}"

    # Append transport section for child events (even if unassigned)
    has_child_attendees = bool(_safe_get_relationship(event, "children"))
    needs_transport = event.drop_off_by or event.pick_up_by or has_child_attendees
    if caregiver_map and needs_transport:
        # Strip any existing transport section before re-appending
        transport_marker = "\n\n🚗 Transport"
        if transport_marker in description:
            description = description[: description.index(transport_marker)]

        drop_off_name = caregiver_map.get(event.drop_off_by, "unassigned") if event.drop_off_by else "unassigned"
        pick_up_name = caregiver_map.get(event.pick_up_by, "unassigned") if event.pick_up_by else "unassigned"
        description += (
            f"\n\n🚗 Transport\n"
            f"Drop-off: {drop_off_name}\n"
            f"Pick-up: {pick_up_name}"
        )

    if description:
        body["description"] = description
    if event.location:
        body["location"] = event.location

    # All-day and time-TBD events use date format; timed events use dateTime format
    if event.all_day or event.time_tbd:
        if event.time_tbd and not event.all_day:
            body["summary"] = event.title + " (time TBD)"
        start_date = event.datetime_start.date()
        body["start"] = {"date": start_date.isoformat()}
        end_date = event.datetime_end.date() if event.datetime_end else start_date + timedelta(days=1)
        body["end"] = {"date": end_date.isoformat()}
    else:
        start_dt = event.datetime_start
        body["start"]["dateTime"] = start_dt.isoformat()
        if start_dt.tzinfo is None:
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

    # Add recurrence rule if this is a recurring event
    if event.rrule:
        from src.utils.rrule import rrule_to_gcal

        body["recurrence"] = rrule_to_gcal(event.rrule)

    # Unconfirmed events show as pending in GCal
    if hasattr(event, 'confirmed_by_caregiver') and not event.confirmed_by_caregiver:
        body["summary"] = "[Pending] " + body["summary"]
        body["transparency"] = "transparent"  # Don't block time

    return body


async def list_upcoming_events_from_gcal(
    session: AsyncSession, family_id: UUID, days: int = 7
) -> list[dict]:
    """Query Google Calendar directly for upcoming events.

    Returns a list of simplified event dicts from GCal.
    Used for reconciliation and as a fallback when local DB has no events.
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
                # Strip display-only prefixes/suffixes we add to GCal titles
                raw_title = item.get("summary", "Untitled")
                clean_title = raw_title.removeprefix("[Pending] ").removesuffix(" (time TBD)")
                events.append({
                    "title": clean_title,
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
    from src.agents.calendar import build_caregiver_name_map
    from src.state import children as children_dal

    caregivers = await families_dal.get_caregivers_for_family(session, family_id)
    logger.debug(
        "create_calendar_event: %d caregivers, tokens: %s",
        len(caregivers),
        [(c.name, bool(c.google_refresh_token_encrypted)) for c in caregivers],
    )
    caregiver_map = build_caregiver_name_map(caregivers)
    children = await children_dal.get_children_for_family(session, family_id)
    child_map = {c.id: c.name for c in children}
    gcal_event_ids: list[str] = []
    last_error: Exception | None = None
    body = event_to_gcal_body(event, caregiver_map=caregiver_map, child_map=child_map)

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
        except Exception as exc:
            logger.exception(
                "Failed to create GCal event for caregiver %s", caregiver.id
            )
            last_error = exc

    if not gcal_event_ids and caregivers:
        raise RuntimeError("Failed to create GCal event for all caregivers") from last_error

    # Store GCal IDs as source_refs on the event (with gcal: prefix for lookup consistency)
    if gcal_event_ids:
        existing_refs = event.source_refs or []
        event.source_refs = existing_refs + [f"gcal:{gid}" for gid in gcal_event_ids]
        await session.flush()

    return gcal_event_ids


async def update_calendar_event(
    session: AsyncSession, family_id: UUID, event: Event
) -> None:
    """Update an event on all connected caregivers' calendars.

    Uses source_refs to find the GCal event IDs to update.
    """
    from src.agents.calendar import build_caregiver_name_map
    from src.state import children as children_dal

    caregivers = await families_dal.get_caregivers_for_family(session, family_id)
    caregiver_map = build_caregiver_name_map(caregivers)
    children = await children_dal.get_children_for_family(session, family_id)
    child_map = {c.id: c.name for c in children}
    body = event_to_gcal_body(event, caregiver_map=caregiver_map, child_map=child_map)
    gcal_ids = _strip_gcal_refs(event.source_refs or [])
    last_error: Exception | None = None
    any_success = False

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
                    any_success = True
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
        except Exception as exc:
            logger.exception(
                "Failed to get credentials for caregiver %s", caregiver.id
            )
            last_error = exc

    if not any_success and gcal_ids and caregivers:
        raise RuntimeError("Failed to update GCal event for all caregivers") from last_error


async def delete_calendar_event(
    session: AsyncSession, family_id: UUID, event: Event
) -> None:
    """Delete an event from all connected caregivers' calendars."""
    caregivers = await families_dal.get_caregivers_for_family(session, family_id)
    gcal_ids = _strip_gcal_refs(event.source_refs or [])

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


async def delete_gcal_event_by_id(
    session: AsyncSession, family_id: UUID, gcal_id: str
) -> None:
    """Delete a single GCal event by its GCal ID from all connected caregivers' calendars."""
    caregivers = await families_dal.get_caregivers_for_family(session, family_id)

    for caregiver in caregivers:
        if caregiver.google_refresh_token_encrypted is None:
            continue

        try:
            credentials = await get_google_credentials(session, caregiver.id)
            service = get_calendar_service(credentials)
            service.events().delete(
                calendarId="primary",
                eventId=gcal_id,
            ).execute()
            logger.info("Deleted GCal event %s for caregiver %s", gcal_id, caregiver.id)
            break  # Only need to delete on one calendar
        except Exception:
            logger.debug(
                "Could not delete GCal event %s for caregiver %s",
                gcal_id,
                caregiver.id,
            )


async def patch_calendar_event(
    session: AsyncSession,
    family_id: UUID,
    gcal_event_id: str,
    patch_body: dict,
) -> None:
    """Patch a single GCal event on all connected caregivers' calendars.

    Args:
        gcal_event_id: Raw GCal event ID (without 'gcal:' prefix).
        patch_body: Dict of fields to patch (e.g. {"description": "..."}).
    """
    # Strip prefix if caller accidentally passes it
    raw_id = gcal_event_id.removeprefix("gcal:")
    caregivers = await families_dal.get_caregivers_for_family(session, family_id)

    for caregiver in caregivers:
        if caregiver.google_refresh_token_encrypted is None:
            continue
        try:
            credentials = await get_google_credentials(session, caregiver.id)
            service = get_calendar_service(credentials)
            service.events().patch(
                calendarId="primary",
                eventId=raw_id,
                body=patch_body,
            ).execute()
            logger.info(
                "Patched GCal event %s for caregiver %s", raw_id, caregiver.id
            )
            break  # Only need to patch on one calendar
        except Exception:
            logger.debug(
                "Could not patch GCal event %s for caregiver %s",
                raw_id,
                caregiver.id,
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
