"""
Storage module for gcal_trisync.

Handles persistent storage of sync tokens and other state data.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_STATE_FILE = '.trisync_state.json'
DEFAULT_LOCK_FILE = '.trisync.lock'


class SyncLock:
    """
    Cross-process lock that prevents overlapping sync runs.

    Two concurrent runs (e.g. a cron tick starting while a slow sync is
    still in progress) both see remote copies as "missing" and create
    duplicates. This uses an advisory OS file lock (flock on POSIX,
    msvcrt on Windows) which is released automatically by the kernel if
    the process dies, so stale locks cannot occur.
    """

    def __init__(self, lock_file: str | Path = DEFAULT_LOCK_FILE):
        """
        Initialize the lock.

        Args:
            lock_file: Path to the lock file
        """
        self.lock_file = Path(lock_file)
        self._fh: Optional[Any] = None

    def acquire(self) -> bool:
        """
        Try to acquire the lock without blocking.

        Returns:
            True if acquired (or already held by this instance),
            False if another process holds it
        """
        if self._fh is not None:
            return True

        fh = open(self.lock_file, 'a+', encoding='utf-8')
        try:
            if os.name == 'nt':
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False

        # Record the holder's PID for diagnostics
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()

        self._fh = fh
        return True

    def release(self) -> None:
        """Release the lock (no-op if not held)."""
        if self._fh is None:
            return

        try:
            if os.name == 'nt':
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None


@dataclass
class CalendarState:
    """
    State information for a single calendar.

    Attributes:
        sync_token: Google Calendar sync token for incremental sync
        last_full_sync: Timestamp of last full sync
        last_sync: Timestamp of last sync (full or incremental)
        events_synced: Count of events processed in last sync
    """
    sync_token: Optional[str] = None
    last_full_sync: Optional[str] = None
    last_sync: Optional[str] = None
    events_synced: int = 0

    def mark_synced(self, token: Optional[str], event_count: int, is_full: bool = False) -> None:
        """
        Update state after a successful sync.

        Args:
            token: New sync token (if any)
            event_count: Number of events processed
            is_full: Whether this was a full sync
        """
        now = datetime.now(timezone.utc).isoformat()
        self.sync_token = token
        self.last_sync = now
        self.events_synced = event_count
        if is_full:
            self.last_full_sync = now

    def invalidate_token(self) -> None:
        """Invalidate the sync token (forces full sync next time)."""
        self.sync_token = None


@dataclass
class SyncState:
    """
    Global sync state for all calendars.

    Attributes:
        version: State file format version
        calendars: Per-calendar state
        created_at: When state was first created
        updated_at: When state was last updated
    """
    version: int = 1
    calendars: dict[str, CalendarState] = field(default_factory=dict)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    def get_calendar_state(self, calendar_name: str) -> CalendarState:
        """
        Get or create state for a calendar.

        Args:
            calendar_name: Name of the calendar

        Returns:
            CalendarState for the calendar
        """
        if calendar_name not in self.calendars:
            self.calendars[calendar_name] = CalendarState()
        return self.calendars[calendar_name]

    def get_sync_token(self, calendar_name: str) -> Optional[str]:
        """
        Get sync token for a calendar.

        Args:
            calendar_name: Name of the calendar

        Returns:
            Sync token or None if not available
        """
        state = self.calendars.get(calendar_name)
        return state.sync_token if state else None

    def to_dict(self) -> dict[str, Any]:
        """Convert state to dictionary for serialization."""
        return {
            'version': self.version,
            'calendars': {
                name: asdict(state)
                for name, state in self.calendars.items()
            },
            'created_at': self.created_at,
            'updated_at': datetime.now(timezone.utc).isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> 'SyncState':
        """
        Create SyncState from dictionary.

        Args:
            data: Dictionary from JSON file

        Returns:
            SyncState instance
        """
        state = cls(
            version=data.get('version', 1),
            created_at=data.get('created_at'),
            updated_at=data.get('updated_at'),
        )

        calendars = data.get('calendars', {})
        for name, cal_data in calendars.items():
            state.calendars[name] = CalendarState(
                sync_token=cal_data.get('sync_token'),
                last_full_sync=cal_data.get('last_full_sync'),
                last_sync=cal_data.get('last_sync'),
                events_synced=cal_data.get('events_synced', 0),
            )

        return state


class StateStorage:
    """
    Handles reading and writing sync state to disk.

    Thread-safe file operations with atomic writes.
    """

    def __init__(self, state_file: str | Path = DEFAULT_STATE_FILE):
        """
        Initialize storage.

        Args:
            state_file: Path to state file
        """
        self.state_file = Path(state_file)
        self._state: Optional[SyncState] = None

    def load(self) -> SyncState:
        """
        Load state from disk.

        Returns:
            SyncState (new empty state if file doesn't exist)
        """
        if self._state is not None:
            return self._state

        if not self.state_file.exists():
            self._state = SyncState(
                created_at=datetime.now(timezone.utc).isoformat()
            )
            return self._state

        try:
            with open(self.state_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                self._state = SyncState.from_dict(data)
                logger.debug(f"Loaded state from {self.state_file}")
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Invalid state file, starting fresh: {e}")
            self._state = SyncState(
                created_at=datetime.now(timezone.utc).isoformat()
            )

        return self._state

    def save(self, state: Optional[SyncState] = None) -> None:
        """
        Save state to disk.

        Uses atomic write (write to temp, then rename).

        Args:
            state: State to save (uses cached state if None)
        """
        if state is not None:
            self._state = state

        if self._state is None:
            return

        # Atomic write: write to temp file, then rename
        temp_file = self.state_file.with_suffix('.tmp')
        try:
            with open(temp_file, 'w', encoding='utf-8') as f:
                json.dump(self._state.to_dict(), f, indent=2)

            # Atomic rename (replace works on both Unix and Windows)
            temp_file.replace(self.state_file)
            logger.debug(f"Saved state to {self.state_file}")

        except OSError as e:
            logger.error(f"Failed to save state: {e}")
            # Clean up temp file if it exists
            if temp_file.exists():
                temp_file.unlink()
            raise

    def get_sync_token(self, calendar_name: str) -> Optional[str]:
        """
        Get sync token for a calendar.

        Args:
            calendar_name: Name of the calendar

        Returns:
            Sync token or None
        """
        state = self.load()
        return state.get_sync_token(calendar_name)

    def update_calendar(
        self,
        calendar_name: str,
        sync_token: Optional[str],
        event_count: int,
        is_full_sync: bool = False
    ) -> None:
        """
        Update state for a calendar after sync.

        Args:
            calendar_name: Name of the calendar
            sync_token: New sync token
            event_count: Number of events processed
            is_full_sync: Whether this was a full sync
        """
        state = self.load()
        cal_state = state.get_calendar_state(calendar_name)
        cal_state.mark_synced(sync_token, event_count, is_full_sync)
        self.save()

    def invalidate_token(self, calendar_name: str) -> None:
        """
        Invalidate sync token for a calendar.

        Forces full sync on next run.

        Args:
            calendar_name: Name of the calendar
        """
        state = self.load()
        if calendar_name in state.calendars:
            state.calendars[calendar_name].invalidate_token()
            self.save()

    def clear(self) -> None:
        """Clear all state (delete state file)."""
        if self.state_file.exists():
            self.state_file.unlink()
        self._state = None
        logger.info("State cleared")
