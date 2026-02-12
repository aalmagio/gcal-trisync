"""
Data models for gcal_trisync.

This module contains the core data structures used throughout the application.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# Valid visibility values for Google Calendar
VALID_VISIBILITIES = frozenset({'default', 'private', 'public', 'confidential'})


@dataclass
class Calendar:
    """
    Represents a configured calendar with its service and metadata.

    Attributes:
        name: Unique identifier for the calendar (e.g., 'WORK', 'PERS')
        calendar_id: Google Calendar ID ('primary' or full ID)
        credentials_file: Path to OAuth credentials JSON file
        token_file: Path to store/retrieve access token
        service: Google Calendar API service object (set after authentication)
        copy_visibility: Optional visibility override for copies to this calendar
    """
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
    """
    Holds synchronization context and state.

    This is the main context object passed through sync operations,
    containing configuration, calendars, and runtime state.

    Attributes:
        config: Configuration dictionary loaded from YAML/JSON
        calendars: Dictionary mapping calendar names to Calendar objects
        known_prefixes: List of known origin prefixes (e.g., '[WORK] ')
        dry_run: If True, simulate sync without making changes
    """
    config: dict[str, Any]
    calendars: dict[str, Calendar] = field(default_factory=dict)
    known_prefixes: list[str] = field(default_factory=list)
    dry_run: bool = False

    def get_calendar(self, name: str) -> Optional[Calendar]:
        """
        Get calendar by name.

        Args:
            name: Calendar name to look up

        Returns:
            Calendar object if found, None otherwise
        """
        return self.calendars.get(name)

    def get_all_calendars(self) -> list[Calendar]:
        """
        Get all configured calendars.

        Returns:
            List of all Calendar objects
        """
        return list(self.calendars.values())

    def add_calendar(self, calendar: Calendar) -> None:
        """
        Add a calendar to the context.

        Args:
            calendar: Calendar object to add
        """
        self.calendars[calendar.name] = calendar
        self._update_prefixes()

    def _update_prefixes(self) -> None:
        """Update known_prefixes based on current calendars."""
        self.known_prefixes = [f"[{name}] " for name in self.calendars.keys()]
