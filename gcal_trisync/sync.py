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
    SyncResult,
    create_event,
    delete_event,
    find_event_by_chain,
    get_event,
    list_events,
    patch_event,
    sync_events,
    update_event,
)
from .metrics import MetricsCollector
from .storage import StateStorage
from .models import Calendar, SyncContext, VALID_VISIBILITIES
from .utils import (
    add_sync_note,
    canonical_event_dict,
    compute_chain_id,
    get_private_meta,
    get_time_window,
    is_original_event,
    set_private_meta,
    strip_sync_note,
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
    chain_id: str,
    metrics: Optional[MetricsCollector] = None
) -> Optional[dict[str, Any]]:
    """
    Create event copy in target calendar with proper visibility.

    Args:
        ctx: Sync context
        target_cal: Target calendar
        source_event: Source event to copy
        origin_name: Name of source calendar
        chain_id: Chain ID for linking
        metrics: Optional metrics collector

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
        # Preserve busy/free state: absent means 'opaque' (busy)
        'transparency': source_event.get('transparency', 'opaque'),
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
        if metrics:
            metrics.record_create(target_cal.name)
        return None

    created = create_event(target_cal.service, target_cal.calendar_id, clone)

    logger.info(
        f"Created in {target_cal.name}: "
        f"'{(source_event.get('summary', '') or '')[:40]}'"
    )

    if metrics:
        metrics.record_create(target_cal.name)

    return created


def update_if_diff(
    ctx: SyncContext,
    cal: Calendar,
    existing: dict[str, Any],
    source_model: dict[str, Any],
    metrics: Optional[MetricsCollector] = None
) -> tuple[dict[str, Any], bool]:
    """
    Update event if it differs from source model.

    Args:
        ctx: Sync context
        cal: Calendar containing the event
        existing: Existing event to potentially update
        source_model: Canonical source data
        metrics: Optional metrics collector

    Returns:
        Tuple of (updated_event, was_changed)
    """
    # Events from Gmail cannot be modified via the API
    if (existing.get('eventType') or '') == 'fromGmail':
        return existing, False

    changed = False
    ex = canonical_event_dict(existing)
    desired = dict(existing)

    for key in ('location', 'start', 'end', 'transparency'):
        if ex.get(key) != source_model.get(key):
            desired[key] = source_model.get(key)
            changed = True

    # Description: compare on the base text (sync note stripped), so the
    # note is kept on copies but never propagated back to the original
    note = ctx.config.get('sync_tag_in_description', '')
    base_desc = strip_sync_note(source_model.get('description', ''), note)

    if is_original_event(existing, cal.name):
        desired_desc = base_desc
    else:
        desired_desc = add_sync_note(base_desc, note)

    if desired_desc != (existing.get('description') or ''):
        desired['description'] = desired_desc
        changed = True

    # Check visibility — only update copies, not original events
    if not is_original_event(existing, cal.name):
        target_vis = desired_copy_visibility(cal, ctx.config)
        if existing.get('visibility') != target_vis:
            desired['visibility'] = target_vis
            changed = True

    if not changed:
        return existing, False

    if ctx.dry_run:
        logger.info(f"[DRY-RUN] Would update in {cal.name}")
        if metrics:
            metrics.record_update(cal.name)
        return existing, True

    try:
        updated = update_event(cal.service, cal.calendar_id, existing['id'], desired)
        if metrics:
            metrics.record_update(cal.name)
        return updated, True
    except HttpError as e:
        logger.error(f"Update failed on {cal.name}: {e}")
        if metrics:
            metrics.record_error(cal.name)
        return existing, False


def perform_safe_delete(
    ctx: SyncContext,
    chain_id: str,
    items: list[tuple[str, dict[str, Any]]],
    metrics: Optional[MetricsCollector] = None
) -> bool:
    """
    Handle safe deletion when origin event is missing.

    Args:
        ctx: Sync context
        chain_id: Chain ID of the event chain
        items: List of (calendar_name, event) tuples
        metrics: Optional metrics collector

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
            if metrics:
                metrics.record_delete(name)
            continue

        try:
            delete_event(cal.service, cal.calendar_id, ev['id'])
            logger.info(f"[chain {chain_id[:8]}] Deleted in {name} (origin missing)")
            if metrics:
                metrics.record_delete(name)
        except HttpError as e:
            logger.error(f"Delete failed on {name}: {e}")
            if metrics:
                metrics.record_error(name)

    return True


def process_unsynced_events(
    ctx: SyncContext,
    unsynced: list[tuple[str, dict[str, Any]]],
    chain_map: dict[str, list[tuple[str, dict[str, Any]]]],
    metrics: Optional[MetricsCollector] = None
) -> None:
    """
    Process events that haven't been synced yet.

    Args:
        ctx: Sync context
        unsynced: List of (calendar_name, event) tuples to process
        chain_map: Chain map to update
        metrics: Optional metrics collector
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
                    if metrics:
                        metrics.record_error(src_name)
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
                create_copy_with_visibility(
                    ctx, cal, ev, src_name, chain_id, metrics=metrics
                )
            except HttpError as e:
                logger.error(f"Insert failed on {cal.name}: {e}")
                if metrics:
                    metrics.record_error(cal.name)

        chain_map.setdefault(chain_id, []).append((src_name, ev))


def _is_self_copy(
    cal_name: str,
    event: dict[str, Any],
    chain_id: str
) -> bool:
    """
    Detect a copy that was mistakenly created on its own origin calendar.

    A genuine original satisfies chain_id == sha256(cal_name:event_id);
    a self-copy claims the calendar as origin but has a different event ID
    and carries the origin prefix in its title.

    Args:
        cal_name: Name of the calendar hosting the event
        event: Event to check
        chain_id: Chain ID of the chain being processed

    Returns:
        True if the event is a spurious self-copy
    """
    priv = get_private_meta(event)
    if priv.get('trisync') != '1':
        return False
    if priv.get('trisync_origin') != cal_name:
        return False
    if priv.get('trisync_chain_id') != chain_id:
        return False
    if compute_chain_id(cal_name, event.get('id', '')) == chain_id:
        return False  # real original
    title = event.get('summary') or ''
    return title.startswith(f"[{cal_name}] ")


def remove_self_copies(
    ctx: SyncContext,
    chain_id: str,
    items: list[tuple[str, dict[str, Any]]],
    metrics: Optional[MetricsCollector] = None
) -> list[tuple[str, dict[str, Any]]]:
    """
    Delete spurious self-copies from their origin calendar.

    Controlled by the 'cleanup_self_copies' config key (default: True).

    Args:
        ctx: Sync context
        chain_id: Chain ID of the chain being processed
        items: List of (calendar_name, event) tuples
        metrics: Optional metrics collector

    Returns:
        Items with removed self-copies filtered out
    """
    if not ctx.config.get('cleanup_self_copies', True):
        return items

    kept: list[tuple[str, dict[str, Any]]] = []

    for name, ev in items:
        if not _is_self_copy(name, ev, chain_id):
            kept.append((name, ev))
            continue

        cal = ctx.get_calendar(name)
        if not cal:
            kept.append((name, ev))
            continue

        if ctx.dry_run:
            logger.info(
                f"[DRY-RUN] Would delete self-copy in {name}: "
                f"'{(ev.get('summary') or '')[:40]}'"
            )
            if metrics:
                metrics.record_delete(name)
            continue

        try:
            delete_event(cal.service, cal.calendar_id, ev['id'])
            logger.info(
                f"[chain {chain_id[:8]}] Deleted self-copy in {name}: "
                f"'{(ev.get('summary') or '')[:40]}'"
            )
            if metrics:
                metrics.record_delete(name)
        except HttpError as e:
            logger.error(f"Self-copy delete failed on {name}: {e}")
            if metrics:
                metrics.record_error(name)
            kept.append((name, ev))

    return kept


def _find_chain_origin(
    items: list[tuple[str, dict[str, Any]]]
) -> Optional[str]:
    """
    Determine the origin calendar of a chain.

    Copies always carry trisync_origin metadata. An event without any
    trisync metadata can only be an original that could not be tagged
    (e.g. fromGmail events, or a failed metadata patch).

    Args:
        items: List of (calendar_name, event) tuples

    Returns:
        Origin calendar name, or None if it cannot be determined
    """
    for _, ev in items:
        origin = get_private_meta(ev).get('trisync_origin')
        if origin:
            return origin

    for name, ev in items:
        if get_private_meta(ev).get('trisync') != '1':
            return name

    return None


def process_chains(
    ctx: SyncContext,
    chain_map: dict[str, list[tuple[str, dict[str, Any]]]],
    metrics: Optional[MetricsCollector] = None
) -> None:
    """
    Process existing event chains for updates and deletions.

    Args:
        ctx: Sync context
        chain_map: Map of chain_id to list of (calendar_name, event)
        metrics: Optional metrics collector
    """
    if metrics:
        metrics.record_chains(len(chain_map))

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

        # Remove copies mistakenly created on their own origin calendar
        # before they can poison safe-delete or source-of-truth selection
        items = remove_self_copies(ctx, chain_id, items, metrics=metrics)

        # Handle safe delete
        if perform_safe_delete(ctx, chain_id, items, metrics=metrics):
            continue

        if not items:
            continue

        chain_origin = _find_chain_origin(items)

        # Find most recently updated event as source of truth
        def get_update_time(ev: dict[str, Any]) -> Any:
            return isoparse(ev.get('updated', '1970-01-01T00:00:00Z'))

        source_name, source_event = max(items, key=lambda t: get_update_time(t[1]))
        model = canonical_event_dict(source_event)

        # Update or create copies in all calendars
        for cal in ctx.get_all_calendars():
            target_ev = find_event_by_chain(cal.service, cal.calendar_id, chain_id)

            if target_ev is None:
                # Never create a copy on the chain's own origin calendar:
                # the original may simply lack metadata there (fromGmail,
                # failed patch) and a missing original is handled by
                # perform_safe_delete — creating one here would duplicate
                # the event on its own calendar with its own prefix
                if chain_origin is not None and cal.name == chain_origin:
                    continue

                # Missing copy, create it
                origin = chain_origin or source_name
                try:
                    create_copy_with_visibility(
                        ctx, cal, source_event, origin, chain_id,
                        metrics=metrics
                    )
                    logger.info(f"[chain {chain_id[:8]}] Created missing in {cal.name}")
                except HttpError as e:
                    logger.error(f"Insert failed on {cal.name}: {e}")
                    if metrics:
                        metrics.record_error(cal.name)
            else:
                # Update existing copy if different
                _, changed = update_if_diff(
                    ctx, cal, target_ev, model, metrics=metrics
                )
                if changed:
                    logger.info(f"[chain {chain_id[:8]}] Updated in {cal.name}")


def run_sync(
    ctx: SyncContext,
    metrics: Optional[MetricsCollector] = None
) -> Optional[MetricsCollector]:
    """
    Run the full synchronization process (legacy mode).

    This fetches ALL events every time. For better performance,
    use run_sync_incremental() instead.

    Args:
        ctx: Sync context with calendars initialized
        metrics: Optional metrics collector (created automatically if None)

    Returns:
        MetricsCollector with sync results, or None if metrics not requested
    """
    if metrics is None:
        metrics = MetricsCollector(dry_run=ctx.dry_run, incremental=False)

    metrics.start()
    time_min, time_max = get_time_window(ctx.config)

    # Fetch all events
    cal_events: dict[str, list[dict[str, Any]]] = {}
    for cal in ctx.get_all_calendars():
        metrics.start_fetch(cal.name)
        evs = list_events(cal.service, cal.calendar_id, time_min, time_max)
        cal_events[cal.name] = evs
        metrics.record_fetch(cal.name, len(evs), sync_type='full')
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
                if should_skip_event(ev, ctx.config, ctx.known_prefixes):
                    metrics.record_skip(cal.name)
                else:
                    unsynced.append((cal.name, ev))

    # Process unsynced events
    process_unsynced_events(ctx, unsynced, chain_map, metrics=metrics)

    # Process existing chains
    process_chains(ctx, chain_map, metrics=metrics)

    metrics.stop()
    logger.info("Sync completed.")
    return metrics


def run_sync_incremental(
    ctx: SyncContext,
    storage: Optional[StateStorage] = None,
    force_full: bool = False,
    metrics: Optional[MetricsCollector] = None
) -> Optional[MetricsCollector]:
    """
    Run incremental synchronization using sync tokens.

    On first run or when tokens expire, performs a full sync.
    Subsequent runs only fetch changed events.

    Args:
        ctx: Sync context with calendars initialized
        storage: State storage for sync tokens (creates default if None)
        force_full: Force a full sync even if tokens are available
        metrics: Optional metrics collector (created automatically if None)

    Returns:
        MetricsCollector with sync results, or None if metrics not requested
    """
    if storage is None:
        storage = StateStorage()

    if metrics is None:
        metrics = MetricsCollector(dry_run=ctx.dry_run, incremental=True)

    metrics.start()
    time_min, time_max = get_time_window(ctx.config)

    # Fetch events from each calendar (incremental if possible)
    cal_results: dict[str, SyncResult] = {}
    cal_events: dict[str, list[dict[str, Any]]] = {}

    for cal in ctx.get_all_calendars():
        sync_token = None if force_full else storage.get_sync_token(cal.name)

        metrics.start_fetch(cal.name)
        result = sync_events(
            cal.service,
            cal.calendar_id,
            sync_token=sync_token,
            time_min=time_min,
            time_max=time_max
        )

        cal_results[cal.name] = result
        cal_events[cal.name] = result.events

        sync_type = "full" if result.is_full_sync else "incremental"
        metrics.record_fetch(cal.name, len(result.events), sync_type=sync_type)
        logger.info(
            f"{cal.name}: {sync_type} sync - "
            f"{len(result.events)} events, "
            f"{len(result.deleted_event_ids)} deleted"
        )

    # Handle deleted events from incremental sync
    for cal in ctx.get_all_calendars():
        result = cal_results[cal.name]
        if not result.is_full_sync and result.deleted_event_ids:
            _handle_deleted_events(
                ctx, cal, result.deleted_event_ids, metrics=metrics
            )

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
                if should_skip_event(ev, ctx.config, ctx.known_prefixes):
                    metrics.record_skip(cal.name)
                else:
                    unsynced.append((cal.name, ev))

    # Process unsynced events
    process_unsynced_events(ctx, unsynced, chain_map, metrics=metrics)

    # Process existing chains
    process_chains(ctx, chain_map, metrics=metrics)

    # Persist sync tokens only after processing completed: saving them
    # before would permanently skip the fetched-but-unprocessed changes
    # if the run crashes mid-way
    if not ctx.dry_run:
        for cal in ctx.get_all_calendars():
            result = cal_results[cal.name]
            storage.update_calendar(
                cal.name,
                result.next_sync_token,
                len(result.events),
                result.is_full_sync
            )

    metrics.stop()
    logger.info("Incremental sync completed.")
    return metrics


def _handle_deleted_events(
    ctx: SyncContext,
    cal: Calendar,
    deleted_ids: list[str],
    metrics: Optional[MetricsCollector] = None
) -> None:
    """
    Handle events deleted from a calendar during incremental sync.

    For origin events the chain ID is deterministic
    (sha256(calendar_name:event_id)), so copies can be located on the
    other calendars directly and deleted — no local index required.
    If the deleted event was a copy, the recomputed chain ID matches
    nothing here and the copy is recreated by regular chain processing.

    Args:
        ctx: Sync context
        cal: Calendar where events were deleted
        deleted_ids: IDs of deleted events
        metrics: Optional metrics collector
    """
    if not ctx.config.get('sync_delete', False):
        return

    for event_id in deleted_ids:
        chain_id = compute_chain_id(cal.name, event_id)

        for other_cal in ctx.get_all_calendars():
            if other_cal.name == cal.name:
                continue

            try:
                found = find_event_by_chain(
                    other_cal.service, other_cal.calendar_id, chain_id
                )
            except HttpError as e:
                logger.error(f"Chain lookup failed on {other_cal.name}: {e}")
                if metrics:
                    metrics.record_error(other_cal.name)
                continue

            if not found:
                continue

            # Only delete events this tool created as copies of the
            # deleted origin
            priv = get_private_meta(found)
            if priv.get('trisync') != '1' or priv.get('trisync_origin') != cal.name:
                continue

            if ctx.dry_run:
                logger.info(
                    f"[DRY-RUN] Would delete in {other_cal.name} "
                    f"(origin deleted on {cal.name}): "
                    f"'{(found.get('summary') or '')[:40]}'"
                )
                if metrics:
                    metrics.record_delete(other_cal.name)
                continue

            try:
                delete_event(other_cal.service, other_cal.calendar_id, found['id'])
                logger.info(
                    f"[chain {chain_id[:8]}] Deleted in {other_cal.name} "
                    f"(origin deleted on {cal.name})"
                )
                if metrics:
                    metrics.record_delete(other_cal.name)
            except HttpError as e:
                logger.error(f"Delete failed on {other_cal.name}: {e}")
                if metrics:
                    metrics.record_error(other_cal.name)
