"""
Synchronization logic for gcal_trisync.

This module contains the core sync algorithms and event processing logic.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from dateutil.parser import isoparse
from googleapiclient.errors import HttpError

from .api import (
    create_event,
    delete_event,
    find_event_by_chain,
    get_event,
    list_events,
    patch_event,
    update_event,
)
from .models import Calendar, SyncContext, VALID_VISIBILITIES
from .utils import (
    add_sync_note,
    canonical_event_dict,
    compute_chain_id,
    get_private_meta,
    get_time_window,
    set_private_meta,
    title_with_origin,
)

logger = logging.getLogger(__name__)


def desired_copy_visibility(
    cal: Calendar,
    global_cfg: dict[str, Any]
) -> str:
    """
    Determine desired visibility for event copies.

    Priority: calendar-specific > global > default (private)

    Args:
        cal: Target calendar
        global_cfg: Global configuration

    Returns:
        Visibility string
    """
    # Per-calendar override
    if cal.copy_visibility in VALID_VISIBILITIES:
        return cal.copy_visibility

    # Global setting
    global_vis = global_cfg.get('default_copy_visibility')
    if global_vis in VALID_VISIBILITIES:
        return global_vis

    # Default fallback
    return 'private'


def should_skip_event(
    event: dict[str, Any],
    cfg: dict[str, Any],
    known_prefixes: list[str]
) -> bool:
    """
    Determine if event should be skipped from sync.

    Args:
        event: Event to check
        cfg: Configuration
        known_prefixes: List of known calendar prefixes

    Returns:
        True if event should be skipped
    """
    title = (event.get('summary') or '').strip()

    # Skip by keyword
    for kw in cfg.get('ignore_if_summary_contains', []):
        if kw and kw.lower() in title.lower():
            return True

    # Skip by event type
    ignore_types = set(cfg.get('ignore_event_types') or [])
    ev_type = (event.get('eventType') or '').strip()
    if ev_type and ev_type in ignore_types:
        return True

    # Skip if already has known prefix
    if cfg.get('skip_if_title_has_known_prefix', True):
        for px in known_prefixes:
            if title.startswith(px):
                return True

    return False


def create_copy_with_visibility(
    ctx: SyncContext,
    target_cal: Calendar,
    source_event: dict[str, Any],
    origin_name: str,
    chain_id: str
) -> Optional[dict[str, Any]]:
    """
    Create event copy in target calendar with proper visibility.

    Args:
        ctx: Sync context
        target_cal: Target calendar
        source_event: Source event to copy
        origin_name: Name of source calendar
        chain_id: Chain ID for linking

    Returns:
        Created event or None if dry-run
    """
    cfg = ctx.config

    clone = {
        'summary': title_with_origin(
            cfg.get('prefix_origin_in_title', True),
            origin_name,
            source_event.get('summary', '')
        ),
        'location': source_event.get('location', ''),
        'description': add_sync_note(
            source_event.get('description', ''),
            cfg.get('sync_tag_in_description', '')
        ),
        'start': source_event.get('start', {}),
        'end': source_event.get('end', {}),
        'visibility': desired_copy_visibility(target_cal, cfg),
        'reminders': source_event.get('reminders', {'useDefault': True}),
    }

    set_private_meta(clone, {
        'trisync': '1',
        'trisync_chain_id': chain_id,
        'trisync_origin': origin_name,
    })

    if ctx.dry_run:
        logger.info(
            f"[DRY-RUN] Would create in {target_cal.name}: "
            f"'{(source_event.get('summary', '') or '')[:40]}'"
        )
        return None

    created = create_event(target_cal.service, target_cal.calendar_id, clone)

    logger.info(
        f"Created in {target_cal.name}: "
        f"'{(source_event.get('summary', '') or '')[:40]}'"
    )

    return created


def update_if_diff(
    ctx: SyncContext,
    cal: Calendar,
    existing: dict[str, Any],
    source_model: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    """
    Update event if it differs from source model.

    Args:
        ctx: Sync context
        cal: Calendar containing the event
        existing: Existing event to potentially update
        source_model: Canonical source data

    Returns:
        Tuple of (updated_event, was_changed)
    """
    changed = False
    ex = canonical_event_dict(existing)
    desired = dict(existing)

    for key in ('location', 'description', 'start', 'end'):
        if ex.get(key) != source_model.get(key):
            desired[key] = source_model.get(key)
            changed = True

    # Ensure sync note is present
    desired['description'] = add_sync_note(
        desired.get('description', ''),
        ctx.config.get('sync_tag_in_description', '')
    )

    # Check visibility
    target_vis = desired_copy_visibility(cal, ctx.config)
    if existing.get('visibility') != target_vis:
        desired['visibility'] = target_vis
        changed = True

    if not changed:
        return existing, False

    if ctx.dry_run:
        logger.info(f"[DRY-RUN] Would update in {cal.name}")
        return existing, True

    try:
        updated = update_event(cal.service, cal.calendar_id, existing['id'], desired)
        return updated, True
    except HttpError as e:
        logger.error(f"Update failed on {cal.name}: {e}")
        return existing, False


def perform_safe_delete(
    ctx: SyncContext,
    chain_id: str,
    items: list[tuple[str, dict[str, Any]]]
) -> bool:
    """
    Handle safe deletion when origin event is missing.

    Args:
        ctx: Sync context
        chain_id: Chain ID of the event chain
        items: List of (calendar_name, event) tuples

    Returns:
        True if origin was missing (chain should not be recreated)
    """
    # Find origin name from metadata
    origin_name = None
    for name, ev in items:
        priv = get_private_meta(ev)
        if 'trisync_origin' in priv:
            origin_name = priv['trisync_origin']
            break

    if not origin_name:
        return False

    # Check if origin calendar still has the event
    origin_present = any(name == origin_name for name, _ in items)
    if origin_present:
        return False

    # Origin is missing
    if not ctx.config.get('sync_delete', False):
        return True

    # Delete synced copies
    for name, ev in items:
        if name == origin_name:
            continue

        cal = ctx.get_calendar(name)
        if not cal:
            continue

        priv = get_private_meta(ev)
        if priv.get('trisync') != '1' or priv.get('trisync_chain_id') != chain_id:
            continue

        if ctx.dry_run:
            logger.info(
                f"[DRY-RUN] Would delete in {name} (origin missing): "
                f"chain {chain_id[:8]}"
            )
            continue

        try:
            delete_event(cal.service, cal.calendar_id, ev['id'])
            logger.info(f"[chain {chain_id[:8]}] Deleted in {name} (origin missing)")
        except HttpError as e:
            logger.error(f"Delete failed on {name}: {e}")

    return True


def process_unsynced_events(
    ctx: SyncContext,
    unsynced: list[tuple[str, dict[str, Any]]],
    chain_map: dict[str, list[tuple[str, dict[str, Any]]]]
) -> None:
    """
    Process events that haven't been synced yet.

    Args:
        ctx: Sync context
        unsynced: List of (calendar_name, event) tuples to process
        chain_map: Chain map to update
    """
    for src_name, ev in unsynced:
        chain_id = compute_chain_id(src_name, ev['id'])
        src_cal = ctx.get_calendar(src_name)

        if not src_cal:
            continue

        # Tag source event (except fromGmail events)
        ev_type = ev.get('eventType') or ''
        if ev_type != 'fromGmail':
            set_private_meta(ev, {
                'trisync': '1',
                'trisync_chain_id': chain_id,
                'trisync_origin': src_name
            })

            if not ctx.dry_run:
                try:
                    patch_event(
                        src_cal.service,
                        src_cal.calendar_id,
                        ev['id'],
                        {'extendedProperties': {'private': get_private_meta(ev)}}
                    )
                except Exception as e:
                    logger.warning(f"Cannot save meta on {src_name}: {e}")
        else:
            logger.info(f"Note: 'fromGmail' event on {src_name}, skipping patch")

        # Create copies in other calendars
        for cal in ctx.get_all_calendars():
            if cal.name == src_name:
                continue

            found = find_event_by_chain(cal.service, cal.calendar_id, chain_id)
            if found:
                continue

            try:
                create_copy_with_visibility(ctx, cal, ev, src_name, chain_id)
            except HttpError as e:
                logger.error(f"Insert failed on {cal.name}: {e}")

        chain_map.setdefault(chain_id, []).append((src_name, ev))


def process_chains(
    ctx: SyncContext,
    chain_map: dict[str, list[tuple[str, dict[str, Any]]]]
) -> None:
    """
    Process existing event chains for updates and deletions.

    Args:
        ctx: Sync context
        chain_map: Map of chain_id to list of (calendar_name, event)
    """
    for chain_id, items in chain_map.items():
        # Refresh events to get latest state
        refreshed: list[tuple[str, dict[str, Any]]] = []
        for name, ev in items:
            cal = ctx.get_calendar(name)
            if not cal:
                continue

            try:
                ev_ref = get_event(cal.service, cal.calendar_id, ev['id'])
            except HttpError:
                ev_ref = ev

            refreshed.append((name, ev_ref))

        items = refreshed

        # Handle safe delete
        if perform_safe_delete(ctx, chain_id, items):
            continue

        if not items:
            continue

        # Find most recently updated event as source of truth
        def get_update_time(ev: dict[str, Any]) -> Any:
            return isoparse(ev.get('updated', '1970-01-01T00:00:00Z'))

        source_name, source_event = max(items, key=lambda t: get_update_time(t[1]))
        model = canonical_event_dict(source_event)

        # Update or create copies in all calendars
        for cal in ctx.get_all_calendars():
            target_ev = find_event_by_chain(cal.service, cal.calendar_id, chain_id)

            if target_ev is None:
                # Missing copy, create it
                origin = get_private_meta(source_event).get('trisync_origin', source_name)
                try:
                    create_copy_with_visibility(ctx, cal, source_event, origin, chain_id)
                    logger.info(f"[chain {chain_id[:8]}] Created missing in {cal.name}")
                except HttpError as e:
                    logger.error(f"Insert failed on {cal.name}: {e}")
            else:
                # Update existing copy if different
                _, changed = update_if_diff(ctx, cal, target_ev, model)
                if changed:
                    logger.info(f"[chain {chain_id[:8]}] Updated in {cal.name}")


def run_sync(ctx: SyncContext) -> None:
    """
    Run the full synchronization process.

    Args:
        ctx: Sync context with calendars initialized
    """
    time_min, time_max = get_time_window(ctx.config)

    # Fetch all events
    cal_events: dict[str, list[dict[str, Any]]] = {}
    for cal in ctx.get_all_calendars():
        evs = list_events(cal.service, cal.calendar_id, time_min, time_max)
        cal_events[cal.name] = evs
        logger.info(f"{cal.name}: found {len(evs)} events")

    # Build chain map and identify unsynced events
    chain_map: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    unsynced: list[tuple[str, dict[str, Any]]] = []

    for cal in ctx.get_all_calendars():
        for ev in cal_events[cal.name]:
            priv = get_private_meta(ev)

            if priv.get('trisync') == '1' and 'trisync_chain_id' in priv:
                chain_id = priv['trisync_chain_id']
                chain_map.setdefault(chain_id, []).append((cal.name, ev))
            else:
                if not should_skip_event(ev, ctx.config, ctx.known_prefixes):
                    unsynced.append((cal.name, ev))

    # Process unsynced events
    process_unsynced_events(ctx, unsynced, chain_map)

    # Process existing chains
    process_chains(ctx, chain_map)

    logger.info("Sync completed.")
