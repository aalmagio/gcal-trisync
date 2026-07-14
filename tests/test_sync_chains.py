"""Tests for chain processing: busy/free propagation and self-copy prevention.

These tests exercise gcal_trisync.sync with mocked API calls.
"""

import sys
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, '.')

from gcal_trisync import sync as sync_mod
from gcal_trisync.api import SyncResult
from gcal_trisync.models import Calendar, SyncContext
from gcal_trisync.sync import (
    _handle_deleted_events,
    _is_self_copy,
    create_copy_with_visibility,
    process_chains,
    remove_self_copies,
    run_sync_incremental,
    update_if_diff,
)
from gcal_trisync.utils import compute_chain_id, strip_sync_note


def make_ctx(config=None, dry_run=False):
    """Build a SyncContext with two calendars (ALMA, WORK) and mock services."""
    ctx = SyncContext(config=config or {}, dry_run=dry_run)
    for name, cal_id in (('ALMA', 'alma@example.com'), ('WORK', 'work@example.com')):
        ctx.add_calendar(Calendar(
            name=name,
            calendar_id=cal_id,
            credentials_file=f'creds/{name}.json',
            token_file=f'tokens/{name}.json',
            service=Mock(name=f'{name}_service'),
        ))
    return ctx


def make_copy(event_id, chain_id, origin, summary, **extra):
    """Build a synced copy with trisync metadata."""
    ev = {
        'id': event_id,
        'summary': summary,
        'visibility': 'private',
        'start': {'dateTime': '2026-07-14T10:00:00Z'},
        'end': {'dateTime': '2026-07-14T11:00:00Z'},
        'updated': '2026-01-01T00:00:00Z',
        'extendedProperties': {
            'private': {
                'trisync': '1',
                'trisync_chain_id': chain_id,
                'trisync_origin': origin,
            }
        },
    }
    ev.update(extra)
    return ev


class TestTransparencySync:
    """Busy/free state must propagate to all calendars."""

    def test_copy_inherits_free_state(self):
        """A free (transparent) original must produce a free copy."""
        ctx = make_ctx()
        target = ctx.get_calendar('WORK')
        source = {
            'summary': 'Ferie',
            'transparency': 'transparent',
            'start': {'date': '2026-08-01'},
            'end': {'date': '2026-08-02'},
        }

        with patch.object(sync_mod, 'create_event') as mock_create:
            create_copy_with_visibility(ctx, target, source, 'ALMA', 'chain1')

        body = mock_create.call_args[0][2]
        assert body['transparency'] == 'transparent'

    def test_copy_defaults_to_busy(self):
        """An original without transparency (busy) must produce a busy copy."""
        ctx = make_ctx()
        target = ctx.get_calendar('WORK')
        source = {
            'summary': 'Riunione',
            'start': {'dateTime': '2026-07-14T10:00:00Z'},
            'end': {'dateTime': '2026-07-14T11:00:00Z'},
        }

        with patch.object(sync_mod, 'create_event') as mock_create:
            create_copy_with_visibility(ctx, target, source, 'ALMA', 'chain1')

        body = mock_create.call_args[0][2]
        assert body['transparency'] == 'opaque'

    def test_update_propagates_transparency_change(self):
        """Changing busy -> free on the source must update the copy."""
        ctx = make_ctx()
        cal = ctx.get_calendar('WORK')
        chain_id = compute_chain_id('ALMA', 'orig1')
        existing = make_copy('copy1', chain_id, 'ALMA', '[ALMA] Riunione')
        source_model = {
            'summary': 'Riunione',
            'location': '',
            'description': '',
            'start': existing['start'],
            'end': existing['end'],
            'transparency': 'transparent',
        }

        with patch.object(sync_mod, 'update_event') as mock_update:
            mock_update.side_effect = lambda svc, cid, eid, body: body
            _, changed = update_if_diff(ctx, cal, existing, source_model)

        assert changed is True
        body = mock_update.call_args[0][3]
        assert body['transparency'] == 'transparent'

    def test_no_update_when_state_matches(self):
        """No API call when busy/free state already matches."""
        ctx = make_ctx()
        cal = ctx.get_calendar('WORK')
        chain_id = compute_chain_id('ALMA', 'orig1')
        existing = make_copy('copy1', chain_id, 'ALMA', '[ALMA] Riunione')
        source_model = {
            'summary': 'Riunione',
            'location': '',
            'description': '',
            'start': existing['start'],
            'end': existing['end'],
            'transparency': 'opaque',
        }

        with patch.object(sync_mod, 'update_event') as mock_update:
            _, changed = update_if_diff(ctx, cal, existing, source_model)

        assert changed is False
        mock_update.assert_not_called()


class TestSyncNoteHandling:
    """The sync note belongs to copies only, never to originals."""

    def test_note_not_leaked_to_original(self):
        """When the source of truth is a copy, its note must be stripped
        before updating the original event."""
        note = 'Sincronizzato da gcal_trisync'
        ctx = make_ctx(config={'sync_tag_in_description': note})
        cal = ctx.get_calendar('ALMA')
        chain_id = compute_chain_id('ALMA', 'orig1')
        original = make_copy('orig1', chain_id, 'ALMA', 'Riunione')
        original['description'] = 'Agenda'
        # Source of truth is the WORK copy, whose description carries the note
        source_model = {
            'summary': '[ALMA] Riunione',
            'location': '',
            'description': f'Agenda\n\n{note}',
            'start': original['start'],
            'end': original['end'],
            'transparency': 'opaque',
        }

        with patch.object(sync_mod, 'update_event') as mock_update:
            _, changed = update_if_diff(ctx, cal, original, source_model)

        assert changed is False
        mock_update.assert_not_called()

    def test_note_added_to_copy(self):
        """Copies must keep the sync note in their description."""
        note = 'Sincronizzato da gcal_trisync'
        ctx = make_ctx(config={'sync_tag_in_description': note})
        cal = ctx.get_calendar('WORK')
        chain_id = compute_chain_id('ALMA', 'orig1')
        existing = make_copy('copy1', chain_id, 'ALMA', '[ALMA] Riunione')
        existing['description'] = ''
        source_model = {
            'summary': 'Riunione',
            'location': '',
            'description': 'Agenda',
            'start': existing['start'],
            'end': existing['end'],
            'transparency': 'opaque',
        }

        with patch.object(sync_mod, 'update_event') as mock_update:
            mock_update.side_effect = lambda svc, cid, eid, body: body
            _, changed = update_if_diff(ctx, cal, existing, source_model)

        assert changed is True
        body = mock_update.call_args[0][3]
        assert body['description'] == f'Agenda\n\n{note}'

    def test_from_gmail_event_never_updated(self):
        """fromGmail events cannot be modified via the API."""
        ctx = make_ctx()
        cal = ctx.get_calendar('ALMA')
        existing = {
            'id': 'gm1',
            'eventType': 'fromGmail',
            'summary': 'Volo',
            'start': {'dateTime': '2026-07-14T10:00:00Z'},
            'end': {'dateTime': '2026-07-14T11:00:00Z'},
        }
        source_model = {
            'summary': 'Volo', 'location': 'Aeroporto', 'description': '',
            'start': existing['start'], 'end': existing['end'],
            'transparency': 'opaque',
        }

        with patch.object(sync_mod, 'update_event') as mock_update:
            _, changed = update_if_diff(ctx, cal, existing, source_model)

        assert changed is False
        mock_update.assert_not_called()


class TestNoSelfCopyOnOrigin:
    """A chain must never create a copy on its own origin calendar."""

    def test_untagged_origin_does_not_get_prefixed_duplicate(self):
        """A fromGmail original (no metadata) must not be duplicated on its
        own calendar with the calendar's own prefix."""
        ctx = make_ctx()
        chain_id = compute_chain_id('ALMA', 'orig1')

        original = {
            'id': 'orig1',
            'summary': 'Volo per Milano',
            'eventType': 'fromGmail',
            'start': {'dateTime': '2026-07-14T10:00:00Z'},
            'end': {'dateTime': '2026-07-14T11:00:00Z'},
            'updated': '2026-01-02T00:00:00Z',
        }
        copy_work = make_copy('copyW', chain_id, 'ALMA', '[ALMA] Volo per Milano')

        chain_map = {chain_id: [('ALMA', original), ('WORK', copy_work)]}

        def refresh(service, cal_id, event_id):
            return {'orig1': original, 'copyW': copy_work}[event_id]

        def found_by_chain(service, cal_id, chain):
            # The original carries no chain metadata, so the search on the
            # origin calendar finds nothing
            if cal_id == 'alma@example.com':
                return None
            return copy_work

        with patch.object(sync_mod, 'get_event', side_effect=refresh), \
             patch.object(sync_mod, 'find_event_by_chain', side_effect=found_by_chain), \
             patch.object(sync_mod, 'create_event') as mock_create, \
             patch.object(sync_mod, 'update_event') as mock_update, \
             patch.object(sync_mod, 'delete_event') as mock_delete:
            mock_update.side_effect = lambda svc, cid, eid, body: body
            process_chains(ctx, chain_map)

        # No event may be created on ALMA (the origin calendar)
        for call in mock_create.call_args_list:
            assert call[0][1] != 'alma@example.com'
        mock_create.assert_not_called()
        mock_delete.assert_not_called()

    def test_missing_copy_still_created_on_other_calendars(self):
        """The origin-calendar guard must not block legitimate copies."""
        ctx = make_ctx()
        chain_id = compute_chain_id('ALMA', 'orig1')
        original = make_copy('orig1', chain_id, 'ALMA', 'Riunione')
        original['updated'] = '2026-01-02T00:00:00Z'

        chain_map = {chain_id: [('ALMA', original)]}

        def refresh(service, cal_id, event_id):
            return original

        def found_by_chain(service, cal_id, chain):
            if cal_id == 'alma@example.com':
                return original
            return None  # copy missing on WORK

        with patch.object(sync_mod, 'get_event', side_effect=refresh), \
             patch.object(sync_mod, 'find_event_by_chain', side_effect=found_by_chain), \
             patch.object(sync_mod, 'create_event') as mock_create, \
             patch.object(sync_mod, 'update_event') as mock_update:
            mock_update.side_effect = lambda svc, cid, eid, body: body
            process_chains(ctx, chain_map)

        mock_create.assert_called_once()
        assert mock_create.call_args[0][1] == 'work@example.com'
        body = mock_create.call_args[0][2]
        assert body['summary'] == '[ALMA] Riunione'


class TestSelfCopyCleanup:
    """Existing spurious self-copies must be detected and removed."""

    def _chain(self):
        chain_id = compute_chain_id('ALMA', 'orig1')
        original = make_copy('orig1', chain_id, 'ALMA', 'Riunione')
        self_copy = make_copy('dup1', chain_id, 'ALMA', '[ALMA] Riunione')
        copy_work = make_copy('copyW', chain_id, 'ALMA', '[ALMA] Riunione')
        return chain_id, original, self_copy, copy_work

    def test_detects_self_copy(self):
        chain_id, original, self_copy, _ = self._chain()
        assert _is_self_copy('ALMA', self_copy, chain_id) is True
        assert _is_self_copy('ALMA', original, chain_id) is False

    def test_copy_on_other_calendar_not_flagged(self):
        chain_id, _, _, copy_work = self._chain()
        assert _is_self_copy('WORK', copy_work, chain_id) is False

    def test_unprefixed_event_not_flagged(self):
        """Without the origin prefix (e.g. a user duplicate) nothing is deleted."""
        chain_id, _, self_copy, _ = self._chain()
        self_copy['summary'] = 'Riunione'
        assert _is_self_copy('ALMA', self_copy, chain_id) is False

    def test_removes_self_copy_and_keeps_rest(self):
        ctx = make_ctx()
        chain_id, original, self_copy, copy_work = self._chain()
        items = [('ALMA', original), ('ALMA', self_copy), ('WORK', copy_work)]

        with patch.object(sync_mod, 'delete_event') as mock_delete:
            kept = remove_self_copies(ctx, chain_id, items)

        mock_delete.assert_called_once()
        assert mock_delete.call_args[0][2] == 'dup1'
        assert ('ALMA', original) in kept
        assert ('WORK', copy_work) in kept
        assert all(ev['id'] != 'dup1' for _, ev in kept)

    def test_dry_run_does_not_delete(self):
        ctx = make_ctx(dry_run=True)
        chain_id, original, self_copy, copy_work = self._chain()
        items = [('ALMA', original), ('ALMA', self_copy), ('WORK', copy_work)]

        with patch.object(sync_mod, 'delete_event') as mock_delete:
            kept = remove_self_copies(ctx, chain_id, items)

        mock_delete.assert_not_called()
        assert all(ev['id'] != 'dup1' for _, ev in kept)

    def test_cleanup_can_be_disabled(self):
        ctx = make_ctx(config={'cleanup_self_copies': False})
        chain_id, original, self_copy, copy_work = self._chain()
        items = [('ALMA', original), ('ALMA', self_copy), ('WORK', copy_work)]

        with patch.object(sync_mod, 'delete_event') as mock_delete:
            kept = remove_self_copies(ctx, chain_id, items)

        mock_delete.assert_not_called()
        assert len(kept) == 3


class TestStripSyncNote:
    """Tests for strip_sync_note() utility."""

    def test_strips_appended_note(self):
        assert strip_sync_note('Agenda\n\nnota', 'nota') == 'Agenda'

    def test_strips_note_only_description(self):
        assert strip_sync_note('nota', 'nota') == ''

    def test_no_note_returns_unchanged(self):
        assert strip_sync_note('Agenda', 'nota') == 'Agenda'

    def test_empty_inputs(self):
        assert strip_sync_note(None, None) == ''
        assert strip_sync_note('Agenda', '') == 'Agenda'


class TestIncrementalDelete:
    """sync_delete must work in incremental mode via deterministic chain IDs."""

    def test_deletes_copies_when_origin_deleted(self):
        """Deleting an origin event must delete its copies elsewhere."""
        ctx = make_ctx(config={'sync_delete': True})
        cal = ctx.get_calendar('ALMA')
        chain_id = compute_chain_id('ALMA', 'orig1')
        copy_work = make_copy('copyW', chain_id, 'ALMA', '[ALMA] Riunione')

        def found(service, cal_id, chain):
            if cal_id == 'work@example.com' and chain == chain_id:
                return copy_work
            return None

        with patch.object(sync_mod, 'find_event_by_chain', side_effect=found), \
             patch.object(sync_mod, 'delete_event') as mock_delete:
            _handle_deleted_events(ctx, cal, ['orig1'])

        mock_delete.assert_called_once()
        assert mock_delete.call_args[0][2] == 'copyW'

    def test_noop_when_sync_delete_disabled(self):
        """Without sync_delete nothing must be touched."""
        ctx = make_ctx(config={'sync_delete': False})
        cal = ctx.get_calendar('ALMA')

        with patch.object(sync_mod, 'find_event_by_chain') as mock_find, \
             patch.object(sync_mod, 'delete_event') as mock_delete:
            _handle_deleted_events(ctx, cal, ['orig1'])

        mock_find.assert_not_called()
        mock_delete.assert_not_called()

    def test_deleted_copy_is_ignored(self):
        """Deleting a copy (not an origin) must not cascade: the recomputed
        chain ID matches nothing on the other calendars."""
        ctx = make_ctx(config={'sync_delete': True})
        cal = ctx.get_calendar('WORK')

        with patch.object(sync_mod, 'find_event_by_chain', return_value=None), \
             patch.object(sync_mod, 'delete_event') as mock_delete:
            _handle_deleted_events(ctx, cal, ['copyW'])

        mock_delete.assert_not_called()

    def test_foreign_origin_not_deleted(self):
        """An event whose metadata points to a different origin is left alone."""
        ctx = make_ctx(config={'sync_delete': True})
        cal = ctx.get_calendar('ALMA')
        chain_id = compute_chain_id('ALMA', 'orig1')
        stranger = make_copy('evX', chain_id, 'WORK', '[WORK] Altro')

        with patch.object(sync_mod, 'find_event_by_chain', return_value=stranger), \
             patch.object(sync_mod, 'delete_event') as mock_delete:
            _handle_deleted_events(ctx, cal, ['orig1'])

        mock_delete.assert_not_called()

    def test_dry_run_does_not_delete(self):
        """Dry-run must only log, never delete."""
        ctx = make_ctx(config={'sync_delete': True}, dry_run=True)
        cal = ctx.get_calendar('ALMA')
        chain_id = compute_chain_id('ALMA', 'orig1')
        copy_work = make_copy('copyW', chain_id, 'ALMA', '[ALMA] Riunione')

        with patch.object(sync_mod, 'find_event_by_chain', return_value=copy_work), \
             patch.object(sync_mod, 'delete_event') as mock_delete:
            _handle_deleted_events(ctx, cal, ['orig1'])

        mock_delete.assert_not_called()


class TestSyncTokenPersistence:
    """Sync tokens must be saved only after processing succeeded."""

    def _result(self):
        return SyncResult(
            events=[], next_sync_token='tok1',
            is_full_sync=True, deleted_event_ids=[]
        )

    def test_tokens_saved_after_successful_run(self):
        ctx = make_ctx()
        storage = Mock()
        storage.get_sync_token.return_value = None

        with patch.object(sync_mod, 'sync_events', return_value=self._result()):
            run_sync_incremental(ctx, storage=storage)

        assert storage.update_calendar.call_count == 2
        saved_tokens = [c[0][1] for c in storage.update_calendar.call_args_list]
        assert saved_tokens == ['tok1', 'tok1']

    def test_tokens_not_saved_if_processing_crashes(self):
        """A crash during chain processing must leave the old tokens in
        place, so the next run re-fetches the unprocessed changes."""
        ctx = make_ctx()
        storage = Mock()
        storage.get_sync_token.return_value = None

        with patch.object(sync_mod, 'sync_events', return_value=self._result()), \
             patch.object(sync_mod, 'process_chains', side_effect=RuntimeError('boom')):
            with pytest.raises(RuntimeError):
                run_sync_incremental(ctx, storage=storage)

        storage.update_calendar.assert_not_called()

    def test_dry_run_never_saves_tokens(self):
        ctx = make_ctx(dry_run=True)
        storage = Mock()
        storage.get_sync_token.return_value = None

        with patch.object(sync_mod, 'sync_events', return_value=self._result()):
            run_sync_incremental(ctx, storage=storage)

        storage.update_calendar.assert_not_called()
