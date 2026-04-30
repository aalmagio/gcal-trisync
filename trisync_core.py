"""
trisync_core.py — Core functions for gcal_trisync (no Google API dependencies)

This module contains pure Python functions that can be tested independently
of the Google Calendar API.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

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


def validate_config(cfg: dict[str, Any], check_files: bool = True) -> None:
    """
    Validate configuration structure and values.

    Args:
        cfg: Configuration dictionary to validate
        check_files: Whether to check if credential files exist

    Raises:
        ConfigValidationError: If configuration is invalid
    """
    import os

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

        if check_files and not os.path.exists(cal['credentials_file']):
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


def is_original_event(event: dict[str, Any], cal_name: str) -> bool:
    """Return True if event is the original (not a synced copy) on the given calendar."""
    return get_private_meta(event).get('trisync_origin') == cal_name


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
