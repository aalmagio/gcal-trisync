#!/usr/bin/env python3
"""
gcal_trisync.py — Bidirectional sync for Google Calendars

Features:
- Filters, prefix-skip, safe delete
- Auth console/local with login_hint and port
- fromGmail handling: do not patch source; optional exclusion via config
- Dry-run mode for testing without modifications

License: MIT
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import yaml
from dateutil.parser import isoparse
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

SCOPES = ['https://www.googleapis.com/auth/calendar']

# Valid visibility values for Google Calendar
VALID_VISIBILITIES = frozenset({'default', 'private', 'public', 'confidential'})


@dataclass
class Calendar:
    """Represents a configured calendar with its service and metadata."""
    name: str
    calendar_id: str
    credentials_file: str
    token_file: str
    service: Any = None
    copy_visibility: Optional[str] = None

    def get_calendar_config(self) -> dict[str, Any]:
        """Return calendar configuration as dictionary."""
        return {
            'name': self.name,
            'calendar_id': self.calendar_id,
            'copy_visibility': self.copy_visibility,
        }


@dataclass
class SyncContext:
    """Holds synchronization context and state."""
    config: dict[str, Any]
    calendars: dict[str, Calendar] = field(default_factory=dict)
    known_prefixes: list[str] = field(default_factory=list)
    dry_run: bool = False

    def get_calendar(self, name: str) -> Optional[Calendar]:
        """Get calendar by name."""
        return self.calendars.get(name)

    def get_all_calendars(self) -> list[Calendar]:
        """Get all configured calendars."""
        return list(self.calendars.values())


class ConfigValidationError(Exception):
    """Raised when configuration validation fails."""
    pass


def validate_config(cfg: dict[str, Any]) -> None:
    """
    Validate configuration structure and values.

    Args:
        cfg: Configuration dictionary to validate

    Raises:
        ConfigValidationError: If configuration is invalid
    """
    if 'calendars' not in cfg:
        raise ConfigValidationError("Configuration must contain 'calendars' section")

    if not isinstance(cfg['calendars'], list) or len(cfg['calendars']) < 2:
        raise ConfigValidationError("At least 2 calendars must be configured")

    required_cal_fields = {'name', 'calendar_id', 'credentials_file', 'token_file'}
    seen_names = set()

    for i, cal in enumerate(cfg['calendars']):
        missing = required_cal_fields - set(cal.keys())
        if missing:
            raise ConfigValidationError(
                f"Calendar {i+1} missing required fields: {missing}"
            )

        if cal['name'] in seen_names:
            raise ConfigValidationError(
                f"Duplicate calendar name: {cal['name']}"
            )
        seen_names.add(cal['name'])

        if not os.path.exists(cal['credentials_file']):
            raise ConfigValidationError(
                f"Credentials file not found: {cal['credentials_file']}"
            )

    # Validate optional fields
    if 'default_copy_visibility' in cfg:
        vis = cfg['default_copy_visibility']
        if vis not in VALID_VISIBILITIES:
            raise ConfigValidationError(
                f"Invalid default_copy_visibility: {vis}. "
                f"Must be one of: {VALID_VISIBILITIES}"
            )

    # Validate numeric fields
    for field_name in ('window_days_past', 'window_days_future'):
        if field_name in cfg:
            try:
                val = int(cfg[field_name])
                if val < 0:
                    raise ValueError("Must be non-negative")
            except (ValueError, TypeError) as e:
                raise ConfigValidationError(
                    f"Invalid {field_name}: {cfg[field_name]} - {e}"
                )


def load_config(path: str) -> dict[str, Any]:
    """
    Load configuration from YAML or JSON file.

    Args:
        path: Path to configuration file

    Returns:
        Configuration dictionary

    Raises:
        ConfigValidationError: If configuration is invalid
        FileNotFoundError: If file doesn't exist
    """
    with open(path, 'r', encoding='utf-8') as f:
        if path.endswith(('.yaml', '.yml')):
            cfg = yaml.safe_load(f)
        else:
            cfg = json.load(f)

    validate_config(cfg)
    return cfg


def ensure_dirs() -> None:
    """Create necessary directories for tokens and credentials."""
    os.makedirs('tokens', exist_ok=True)
    os.makedirs('creds', exist_ok=True)


def get_service(
    credentials_file: str,
    token_file: str,
    auth_method: str = 'local',
    login_hint: Optional[str] = None,
    port: int = 0
) -> Any:
    """
    Initialize and return Google Calendar API service.

    Args:
        credentials_file: Path to OAuth credentials JSON
        token_file: Path to store/retrieve access token
        auth_method: 'local' for browser flow, 'console' for manual code entry
        login_hint: Email to pre-fill in login
        port: Port for local OAuth server (0 for auto-select)

    Returns:
        Google Calendar API service object
    """
    creds = None

    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                credentials_file, SCOPES, redirect_uri=None
            )

            if auth_method == 'console':
                auth_url, _ = flow.authorization_url(
                    access_type='offline',
                    include_granted_scopes='true',
                    prompt='consent',
                    login_hint=login_hint
                )
                print("\nOpen this URL in an incognito window and paste the code here:\n")
                print(auth_url)
                code = input("\nCode: ").strip()
                flow.fetch_token(code=code)
                creds = flow.credentials
            else:
                creds = flow.run_local_server(
                    port=port,
                    prompt='consent',
                    authorization_prompt_message=None,
                    login_hint=login_hint
                )

        with open(token_file, 'w', encoding='utf-8') as token:
            token.write(creds.to_json())

    return build('calendar', 'v3', credentials=creds, cache_discovery=False)


def iso(dt: datetime) -> str:
    """Convert datetime to ISO format string with UTC timezone."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def canonical_event_dict(event: dict[str, Any]) -> dict[str, Any]:
    """
    Extract canonical fields from event for comparison.

    Args:
        event: Google Calendar event dictionary

    Returns:
        Dictionary with only comparable fields
    """
    return {
        'summary': event.get('summary', ''),
        'location': event.get('location', ''),
        'description': event.get('description', ''),
        'start': event.get('start', {}),
        'end': event.get('end', {}),
    }


def compute_chain_id(origin_calendar_name: str, event_id: str) -> str:
    """
    Compute unique chain ID for event linking across calendars.

    Uses SHA-256 hash of calendar name and event ID.

    Args:
        origin_calendar_name: Name of the source calendar
        event_id: Google Calendar event ID

    Returns:
        Hexadecimal hash string
    """
    data = f"{origin_calendar_name}:{event_id}".encode('utf-8')
    return hashlib.sha256(data).hexdigest()


def get_private_meta(event: dict[str, Any]) -> dict[str, str]:
    """Extract private extended properties from event."""
    return (event.get('extendedProperties', {}) or {}).get('private', {}) or {}


def set_private_meta(event: dict[str, Any], metadata: dict[str, str]) -> None:
    """
    Set private extended properties on event (in-place).

    Args:
        event: Event dictionary to modify
        metadata: Metadata key-value pairs to set
    """
    ep = event.get('extendedProperties', {}) or {}
    priv = ep.get('private', {}) or {}
    priv.update(metadata)
    ep['private'] = priv
    event['extendedProperties'] = ep


def title_with_origin(prefix_enabled: bool, origin_name: str, title: str) -> str:
    """
    Add origin prefix to event title if enabled.

    Args:
        prefix_enabled: Whether to add prefix
        origin_name: Calendar name for prefix
        title: Original event title

    Returns:
        Title with or without prefix
    """
    if not prefix_enabled:
        return title or ''

    prefix = f"[{origin_name}] "
    t = title or ''

    if t.startswith(prefix):
        return t
    return prefix + t


def get_time_window(cfg: dict[str, Any]) -> tuple[str, str]:
    """
    Calculate sync time window from configuration.

    Args:
        cfg: Configuration dictionary

    Returns:
        Tuple of (time_min, time_max) as ISO strings
    """
    now = datetime.now(timezone.utc)
    tmin = now - timedelta(days=int(cfg.get('window_days_past', 30)))
    tmax = now + timedelta(days=int(cfg.get('window_days_future', 365)))
    return iso(tmin), iso(tmax)


def list_events(
    service: Any,
    calendar_id: str,
    time_min: str,
    time_max: str
) -> list[dict[str, Any]]:
    """
    List all events in a calendar within the time window.

    Args:
        service: Google Calendar API service
        calendar_id: Calendar ID to query
        time_min: Start of time window (ISO format)
        time_max: End of time window (ISO format)

    Returns:
        List of event dictionaries
    """
    events = []
    page_token = None

    while True:
        resp = service.events().list(
            calendarId=calendar_id,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy='startTime',
            pageToken=page_token,
            maxResults=2500
        ).execute()

        events.extend(resp.get('items', []))
        page_token = resp.get('nextPageToken')

        if not page_token:
            break

    return events


def find_event_by_chain(
    service: Any,
    calendar_id: str,
    chain_id: str
) -> Optional[dict[str, Any]]:
    """
    Find event by chain ID in private extended properties.

    Args:
        service: Google Calendar API service
        calendar_id: Calendar to search
        chain_id: Chain ID to find

    Returns:
        Event dictionary if found, None otherwise
    """
    resp = service.events().list(
        calendarId=calendar_id,
        privateExtendedProperty=f"trisync_chain_id={chain_id}",
        maxResults=2,
        singleEvents=True
    ).execute()

    items = resp.get('items', [])
    return items[0] if items else None


def add_sync_note(description: str, note: str) -> str:
    """
    Add sync note to event description if not already present.

    Args:
        description: Original description
        note: Note to add

    Returns:
        Description with note appended
    """
    description = description or ''
    note = note or ''

    if note and note not in description:
        if description.strip():
            return description + "\n\n" + note
        return note

    return description


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

    created = target_cal.service.events().insert(
        calendarId=target_cal.calendar_id,
        body=clone
    ).execute()

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
        updated = cal.service.events().update(
            calendarId=cal.calendar_id,
            eventId=existing['id'],
            body=desired
        ).execute()
        return updated, True
    except HttpError as e:
        logger.error(f"Update failed on {cal.name}: {e}")
        return existing, False


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
            cal.service.events().delete(
                calendarId=cal.calendar_id,
                eventId=ev['id']
            ).execute()
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
                    src_cal.service.events().patch(
                        calendarId=src_cal.calendar_id,
                        eventId=ev['id'],
                        body={'extendedProperties': {'private': get_private_meta(ev)}}
                    ).execute()
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
        refreshed = []
        for name, ev in items:
            cal = ctx.get_calendar(name)
            if not cal:
                continue

            try:
                ev_ref = cal.service.events().get(
                    calendarId=cal.calendar_id,
                    eventId=ev['id']
                ).execute()
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
        def get_update_time(ev: dict[str, Any]) -> datetime:
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


def main() -> None:
    """Main entry point for gcal_trisync."""
    parser = argparse.ArgumentParser(
        description="Bidirectional sync for Google Calendars with safe delete."
    )
    parser.add_argument(
        '--config',
        required=True,
        help='Path to config.yaml or config.json'
    )
    parser.add_argument(
        '--auth',
        choices=['local', 'console'],
        default='local',
        help='Authentication method'
    )
    parser.add_argument(
        '--login-hint',
        dest='login_hint',
        default=None,
        help='Email to pre-fill in login'
    )
    parser.add_argument(
        '--port',
        type=int,
        default=0,
        help='Port for local OAuth server'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Simulate sync without making changes'
    )
    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Enable verbose logging'
    )
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.dry_run:
        logger.info("Running in DRY-RUN mode - no changes will be made")

    # Load and validate configuration
    try:
        cfg = load_config(args.config)
    except ConfigValidationError as e:
        logger.error(f"Configuration error: {e}")
        sys.exit(1)
    except FileNotFoundError:
        logger.error(f"Configuration file not found: {args.config}")
        sys.exit(1)

    ensure_dirs()
    time_min, time_max = get_time_window(cfg)

    # Initialize sync context
    ctx = SyncContext(config=cfg, dry_run=args.dry_run)

    # Initialize calendars
    for c in cfg['calendars']:
        svc = get_service(
            c['credentials_file'],
            c['token_file'],
            auth_method=args.auth,
            login_hint=args.login_hint,
            port=args.port
        )

        cal = Calendar(
            name=c['name'],
            calendar_id=c['calendar_id'],
            credentials_file=c['credentials_file'],
            token_file=c['token_file'],
            service=svc,
            copy_visibility=c.get('copy_visibility')
        )
        ctx.calendars[c['name']] = cal

    ctx.known_prefixes = [f"[{name}] " for name in ctx.calendars.keys()]

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
                if not should_skip_event(ev, cfg, ctx.known_prefixes):
                    unsynced.append((cal.name, ev))

    # Process unsynced events
    process_unsynced_events(ctx, unsynced, chain_map)

    # Process existing chains
    process_chains(ctx, chain_map)

    logger.info("Done.")


if __name__ == '__main__':
    main()
