"""Periodic reconciliation between local DB events and Google Calendar.

Runs on a configurable interval (default 60 min) and resolves discrepancies
between the authoritative local DB and GCal.

Conflict resolution:
  - source=calendar (originated from GCal) → GCal wins, update local
  - Any other source → local DB wins, enqueue outbox update to GCal
  - GCal event deleted externally → soft-delete locally
  - GCal event with no local match → import (with dedup check)
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.state.models import EventSource, GcalOutboxOperation

logger = logging.getLogger(__name__)


async def reconcile_loop() -> None:
    """Long-running loop that periodically reconciles local DB with GCal."""
    interval = settings.reconcile_interval_minutes
    if interval <= 0:
        logger.info("GCal reconciliation disabled (interval=0)")
        return

    logger.info("GCal reconciler started (every %d min)", interval)
    while True:
        try:
            await reconcile_all_families()
        except asyncio.CancelledError:
            logger.info("GCal reconciler shutting down")
            raise
        except Exception:
            logger.exception("Reconciler loop error")

        await asyncio.sleep(interval * 60)


async def reconcile_all_families() -> dict:
    """Run reconciliation for all families with connected Google accounts."""
    from src.db import async_session_factory
    from src.state import families as families_dal

    totals = {"families": 0, "created": 0, "updated": 0, "cancelled": 0, "pushed": 0, "skipped": 0}

    async with async_session_factory() as session:
        async with session.begin():
            families = await families_dal.get_families_with_google(session)

    for family in families:
        try:
            async with async_session_factory() as session:
                async with session.begin():
                    stats = await reconcile_family(session, family.id)
                    totals["families"] += 1
                    for key in ["created", "updated", "cancelled", "pushed", "skipped"]:
                        totals[key] += stats.get(key, 0)
        except Exception:
            logger.exception("Reconciliation failed for family %s", family.id)

    logger.info(
        "Reconciliation complete: %d families, %d created, %d updated, %d cancelled, %d pushed",
        totals["families"], totals["created"], totals["updated"],
        totals["cancelled"], totals["pushed"],
    )
    return totals


async def reconcile_family(
    session: AsyncSession, family_id: UUID
) -> dict:
    """Compare local DB events with GCal for one family. Returns stats."""
    from src.actions.gcal import _strip_gcal_refs, list_upcoming_events_from_gcal
    from src.state import events as events_dal
    from src.state import outbox as outbox_dal
    from src.state.models import Event

    stats = {"created": 0, "updated": 0, "cancelled": 0, "pushed": 0, "skipped": 0}

    # Fetch local events for next 30 days
    now = datetime.now(UTC)
    end = now + timedelta(days=30)
    local_events = await events_dal.get_events_in_range(session, family_id, now, end)

    # Build map: raw gcal_id → local event
    local_by_gcal_id: dict[str, Event] = {}
    for ev in local_events:
        for raw_id in _strip_gcal_refs(ev.source_refs or []):
            local_by_gcal_id[raw_id] = ev

    # Fetch GCal events for next 30 days
    gcal_events = await list_upcoming_events_from_gcal(session, family_id, days=30)
    gcal_by_id: dict[str, dict] = {}
    for gev in gcal_events:
        gcal_id = gev.get("gcal_id")
        if gcal_id:
            gcal_by_id[gcal_id] = gev

    # Case 1: Local event has gcal_id but GCal event is gone → soft-delete
    for gcal_id, local_ev in local_by_gcal_id.items():
        if gcal_id not in gcal_by_id:
            desc = local_ev.description or ""
            if "[CANCELLED]" not in desc:
                local_ev.description = desc + "\n[CANCELLED]"
                stats["cancelled"] += 1
                logger.info(
                    "Soft-deleted local event %s (GCal %s no longer exists)",
                    local_ev.id, gcal_id,
                )

    # Case 2: GCal event exists with no local match → import
    for gcal_id, gev in gcal_by_id.items():
        if gcal_id not in local_by_gcal_id:
            # Safety check: query DB for this gcal ref in case it was created
            # by a concurrent reconciliation or webhook handler
            ref_check = await events_dal.get_events_by_source_ref(
                session, family_id, f"gcal:{gcal_id}"
            )
            if ref_check:
                stats["skipped"] += 1
                continue

            title = gev.get("title", "Untitled")
            start_str = gev.get("start", "")
            try:
                dt_start = datetime.fromisoformat(start_str)
            except (ValueError, TypeError):
                stats["skipped"] += 1
                continue

            # Dedup against local events already in memory
            dup = _find_local_duplicate(local_events, title, dt_start)
            if dup:
                existing_refs = dup.source_refs or []
                new_ref = f"gcal:{gcal_id}"
                if new_ref not in existing_refs:
                    dup.source_refs = existing_refs + [new_ref]
                stats["skipped"] += 1
                continue

            # Import as new event
            end_str = gev.get("end")
            dt_end = None
            if end_str:
                try:
                    dt_end = datetime.fromisoformat(end_str)
                except (ValueError, TypeError):
                    pass

            await events_dal.create_event(
                session, family_id,
                source=EventSource.calendar,
                source_refs=[f"gcal:{gcal_id}"],
                title=title,
                description=gev.get("description"),
                datetime_start=dt_start,
                datetime_end=dt_end,
                location=gev.get("location"),
            )
            stats["created"] += 1
            logger.info("Imported GCal event %s ('%s') to local DB", gcal_id, title)

    # Case 3: Both exist — check for discrepancies
    for gcal_id, local_ev in local_by_gcal_id.items():
        if gcal_id not in gcal_by_id:
            continue  # Already handled in Case 1
        gev = gcal_by_id[gcal_id]

        has_diff = _has_discrepancy(local_ev, gev)
        if has_diff:
            if local_ev.source == EventSource.calendar:
                # GCal wins for calendar-sourced events
                gcal_title = gev.get("title")
                if gcal_title:
                    local_ev.title = gcal_title
                local_ev.location = gev.get("location")
                local_ev.description = gev.get("description") or local_ev.description
                start_str = gev.get("start", "")
                try:
                    local_ev.datetime_start = datetime.fromisoformat(start_str)
                except (ValueError, TypeError):
                    pass
                stats["updated"] += 1
            else:
                # Local DB wins — push to GCal
                await outbox_dal.enqueue_gcal_write(
                    session, family_id, local_ev.id, GcalOutboxOperation.update, {},
                    idempotency_key=f"reconcile:{local_ev.id}:{uuid4().hex[:12]}",
                )
                stats["pushed"] += 1

    await session.flush()
    return stats


def _has_discrepancy(local_ev: "Event", gev: dict) -> bool:
    """Check if a local event and GCal event have meaningful differences."""
    gcal_title = gev.get("title", "")
    if gcal_title and local_ev.title != gcal_title:
        return True

    gcal_location = gev.get("location")
    if gcal_location != local_ev.location:
        return True

    start_str = gev.get("start", "")
    if start_str:
        try:
            gcal_start = datetime.fromisoformat(start_str)
            if local_ev.datetime_start and abs((local_ev.datetime_start - gcal_start).total_seconds()) > 60:
                return True
        except (ValueError, TypeError):
            pass

    return False


def _find_local_duplicate(
    events: list, title: str, dt_start: datetime, threshold_minutes: int = 30
) -> object | None:
    """Find a duplicate among already-loaded local events (avoids N+1 DB queries)."""
    from src.state.events import compute_title_similarity

    window_start = dt_start - timedelta(minutes=threshold_minutes)
    window_end = dt_start + timedelta(minutes=threshold_minutes)

    for ev in events:
        if ev.datetime_start and window_start <= ev.datetime_start <= window_end:
            if compute_title_similarity(title, ev.title) >= 0.7:
                return ev
    return None
