"""
Utility functions for gcal_trisync.

This module contains pure utility functions with no external dependencies.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any


def iso(dt: datetime) -> str:
    """
    Convert datetime to ISO format string with UTC timezone.

    Args:
        dt: Datetime object to convert

    Returns:
        ISO 8601 formatted string
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def compute_chain_id(origin_calendar_name: str, event_id: str) -> str:
    """
    Compute unique chain ID for event linking across calendars.

    Uses SHA-256 hash of calendar name and event ID to create a
    unique, deterministic identifier for each event chain.

    Args:
        origin_calendar_name: Name of the source calendar
        event_id: Google Calendar event ID

    Returns:
        Hexadecimal hash string (64 characters)
    """
    data = f"{origin_calendar_name}:{event_id}".encode('utf-8')
    return hashlib.sha256(data).hexdigest()


def canonical_event_dict(event: dict[str, Any]) -> dict[str, Any]:
    """
    Extract canonical fields from event for comparison.

    Only includes fields that should be compared for sync purposes,
    excluding metadata, IDs, and other non-content fields.

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


def get_private_meta(event: dict[str, Any]) -> dict[str, str]:
    """
    Extract private extended properties from event.

    Args:
        event: Google Calendar event dictionary

    Returns:
        Dictionary of private metadata, empty dict if none
    """
    return (event.get('extendedProperties', {}) or {}).get('private', {}) or {}


def set_private_meta(event: dict[str, Any], metadata: dict[str, str]) -> None:
    """
    Set private extended properties on event (in-place).

    Creates the extendedProperties structure if it doesn't exist.

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
        origin_name: Calendar name for prefix (e.g., 'WORK')
        title: Original event title

    Returns:
        Title with or without prefix (e.g., '[WORK] Meeting')
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
        cfg: Configuration dictionary with optional window_days_past
             and window_days_future keys

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
        description: Original description (may be None)
        note: Note to add (may be None or empty)

    Returns:
        Description with note appended, or original if note already present
    """
    description = description or ''
    note = note or ''

    if note and note not in description:
        if description.strip():
            return description + "\n\n" + note
        return note

    return description
