"""Tests for state storage in gcal_trisync."""

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from importlib.util import spec_from_file_location, module_from_spec
from pathlib import Path

import pytest

# Import storage module directly to avoid Google API dependencies
_storage_path = Path(__file__).parent.parent / 'gcal_trisync' / 'storage.py'
_spec = spec_from_file_location('gcal_trisync.storage', _storage_path)
_storage = module_from_spec(_spec)
sys.modules['gcal_trisync.storage'] = _storage
_spec.loader.exec_module(_storage)

CalendarState = _storage.CalendarState
StateStorage = _storage.StateStorage
SyncState = _storage.SyncState
DEFAULT_STATE_FILE = _storage.DEFAULT_STATE_FILE


class TestCalendarState:
    """Tests for CalendarState dataclass."""

    def test_create_empty_state(self):
        """Should create state with None values."""
        state = CalendarState()

        assert state.sync_token is None
        assert state.last_full_sync is None
        assert state.last_sync is None
        assert state.events_synced == 0

    def test_mark_synced(self):
        """Should update state after sync."""
        state = CalendarState()

        state.mark_synced(token='abc123', event_count=50, is_full=True)

        assert state.sync_token == 'abc123'
        assert state.events_synced == 50
        assert state.last_sync is not None
        assert state.last_full_sync is not None

    def test_mark_synced_incremental(self):
        """Should not update last_full_sync for incremental sync."""
        state = CalendarState()
        state.mark_synced(token='token1', event_count=100, is_full=True)
        full_sync_time = state.last_full_sync

        state.mark_synced(token='token2', event_count=5, is_full=False)

        assert state.sync_token == 'token2'
        assert state.events_synced == 5
        assert state.last_full_sync == full_sync_time  # unchanged

    def test_invalidate_token(self):
        """Should clear sync token."""
        state = CalendarState(sync_token='abc123')

        state.invalidate_token()

        assert state.sync_token is None


class TestSyncState:
    """Tests for SyncState dataclass."""

    def test_create_empty_state(self):
        """Should create state with defaults."""
        state = SyncState()

        assert state.version == 1
        assert state.calendars == {}

    def test_get_calendar_state_creates(self):
        """Should create calendar state if not exists."""
        state = SyncState()

        cal_state = state.get_calendar_state('WORK')

        assert 'WORK' in state.calendars
        assert isinstance(cal_state, CalendarState)

    def test_get_calendar_state_returns_existing(self):
        """Should return existing calendar state."""
        state = SyncState()
        state.calendars['WORK'] = CalendarState(sync_token='existing')

        cal_state = state.get_calendar_state('WORK')

        assert cal_state.sync_token == 'existing'

    def test_get_sync_token(self):
        """Should get sync token for calendar."""
        state = SyncState()
        state.calendars['WORK'] = CalendarState(sync_token='token123')

        assert state.get_sync_token('WORK') == 'token123'
        assert state.get_sync_token('NONEXISTENT') is None

    def test_to_dict(self):
        """Should convert state to dictionary."""
        state = SyncState()
        state.calendars['WORK'] = CalendarState(sync_token='token', events_synced=10)

        result = state.to_dict()

        assert result['version'] == 1
        assert 'WORK' in result['calendars']
        assert result['calendars']['WORK']['sync_token'] == 'token'
        assert result['updated_at'] is not None

    def test_from_dict(self):
        """Should create state from dictionary."""
        data = {
            'version': 1,
            'calendars': {
                'WORK': {
                    'sync_token': 'token123',
                    'events_synced': 25,
                },
                'PERS': {
                    'sync_token': 'token456',
                    'events_synced': 15,
                },
            },
            'created_at': '2024-01-01T00:00:00Z',
        }

        state = SyncState.from_dict(data)

        assert state.version == 1
        assert len(state.calendars) == 2
        assert state.calendars['WORK'].sync_token == 'token123'
        assert state.calendars['PERS'].events_synced == 15


class TestStateStorage:
    """Tests for StateStorage class."""

    @pytest.fixture
    def temp_state_file(self):
        """Create a temporary state file path."""
        fd, path = tempfile.mkstemp(suffix='.json')
        os.close(fd)
        os.unlink(path)  # Delete so we start fresh
        yield path
        if os.path.exists(path):
            os.unlink(path)

    def test_load_creates_new_state(self, temp_state_file):
        """Should create new state if file doesn't exist."""
        storage = StateStorage(temp_state_file)

        state = storage.load()

        assert isinstance(state, SyncState)
        assert state.created_at is not None

    def test_save_and_load(self, temp_state_file):
        """Should save and reload state correctly."""
        storage = StateStorage(temp_state_file)
        state = storage.load()
        state.calendars['WORK'] = CalendarState(sync_token='mytoken')
        storage.save(state)

        # Create new storage instance to force reload
        storage2 = StateStorage(temp_state_file)
        loaded = storage2.load()

        assert loaded.calendars['WORK'].sync_token == 'mytoken'

    def test_get_sync_token(self, temp_state_file):
        """Should get sync token for calendar."""
        storage = StateStorage(temp_state_file)
        state = storage.load()
        state.calendars['WORK'] = CalendarState(sync_token='token123')
        storage.save()

        # New storage instance
        storage2 = StateStorage(temp_state_file)
        token = storage2.get_sync_token('WORK')

        assert token == 'token123'

    def test_get_sync_token_nonexistent(self, temp_state_file):
        """Should return None for nonexistent calendar."""
        storage = StateStorage(temp_state_file)

        token = storage.get_sync_token('NONEXISTENT')

        assert token is None

    def test_update_calendar(self, temp_state_file):
        """Should update calendar state and save."""
        storage = StateStorage(temp_state_file)

        storage.update_calendar('WORK', 'newtoken', 50, is_full_sync=True)

        # Verify saved
        storage2 = StateStorage(temp_state_file)
        state = storage2.load()

        assert state.calendars['WORK'].sync_token == 'newtoken'
        assert state.calendars['WORK'].events_synced == 50
        assert state.calendars['WORK'].last_full_sync is not None

    def test_invalidate_token(self, temp_state_file):
        """Should invalidate token and save."""
        storage = StateStorage(temp_state_file)
        storage.update_calendar('WORK', 'mytoken', 10)

        storage.invalidate_token('WORK')

        # Verify
        storage2 = StateStorage(temp_state_file)
        assert storage2.get_sync_token('WORK') is None

    def test_clear(self, temp_state_file):
        """Should delete state file."""
        storage = StateStorage(temp_state_file)
        storage.update_calendar('WORK', 'token', 10)

        assert os.path.exists(temp_state_file)

        storage.clear()

        assert not os.path.exists(temp_state_file)

    def test_load_corrupted_file(self, temp_state_file):
        """Should handle corrupted JSON gracefully."""
        with open(temp_state_file, 'w') as f:
            f.write('not valid json {{{')

        storage = StateStorage(temp_state_file)
        state = storage.load()

        # Should create fresh state
        assert isinstance(state, SyncState)
        assert state.calendars == {}

    def test_atomic_write(self, temp_state_file):
        """Should write atomically (no temp file left on success)."""
        storage = StateStorage(temp_state_file)
        storage.update_calendar('WORK', 'token', 10)

        temp_path = Path(temp_state_file).with_suffix('.tmp')
        assert not temp_path.exists()
        assert Path(temp_state_file).exists()


SyncLock = _storage.SyncLock


class TestSyncLock:
    """Tests for the SyncLock cross-process lock."""

    def test_acquire_and_release(self, tmp_path):
        """Lock can be acquired, released, and re-acquired."""
        lock = SyncLock(tmp_path / 'test.lock')
        assert lock.acquire() is True
        lock.release()
        assert lock.acquire() is True
        lock.release()

    def test_acquire_is_idempotent_for_holder(self):
        """A holder re-acquiring its own lock succeeds."""
        with tempfile.TemporaryDirectory() as d:
            lock = SyncLock(Path(d) / 'test.lock')
            assert lock.acquire() is True
            assert lock.acquire() is True
            lock.release()

    def test_second_instance_cannot_acquire(self, tmp_path):
        """A second lock on the same file must fail while held."""
        first = SyncLock(tmp_path / 'test.lock')
        second = SyncLock(tmp_path / 'test.lock')

        assert first.acquire() is True
        assert second.acquire() is False

        first.release()
        assert second.acquire() is True
        second.release()

    def test_release_without_acquire_is_noop(self, tmp_path):
        """Releasing a never-acquired lock must not raise."""
        lock = SyncLock(tmp_path / 'test.lock')
        lock.release()

    def test_lock_file_records_pid(self, tmp_path):
        """The lock file contains the holder's PID for diagnostics."""
        lock_file = tmp_path / 'test.lock'
        lock = SyncLock(lock_file)
        assert lock.acquire() is True

        assert lock_file.read_text() == str(os.getpid())
        lock.release()

    def test_different_files_do_not_conflict(self, tmp_path):
        """Locks on different files are independent."""
        a = SyncLock(tmp_path / 'a.lock')
        b = SyncLock(tmp_path / 'b.lock')
        assert a.acquire() is True
        assert b.acquire() is True
        a.release()
        b.release()
