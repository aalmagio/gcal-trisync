"""Tests for sync logic in gcal_trisync."""

import pytest
from unittest.mock import Mock, MagicMock, patch
from dataclasses import dataclass

import sys
sys.path.insert(0, '.')

from trisync_core import (
    should_skip_event,
    desired_copy_visibility,
    Calendar,
    SyncContext,
    VALID_VISIBILITIES,
)


class TestShouldSkipEvent:
    """Tests for should_skip_event() function."""

    def test_skip_by_keyword_match(self):
        """Should skip event when summary contains ignored keyword."""
        cfg = {'ignore_if_summary_contains': ['birthday', 'holiday']}
        event = {'summary': "John's birthday party"}

        assert should_skip_event(event, cfg, []) is True

    def test_skip_keyword_case_insensitive(self):
        """Keyword matching should be case-insensitive."""
        cfg = {'ignore_if_summary_contains': ['Birthday']}
        event = {'summary': 'BIRTHDAY celebration'}

        assert should_skip_event(event, cfg, []) is True

    def test_no_skip_without_keyword(self):
        """Should not skip when keyword not present."""
        cfg = {'ignore_if_summary_contains': ['birthday']}
        event = {'summary': 'Team meeting'}

        assert should_skip_event(event, cfg, []) is False

    def test_skip_by_event_type(self):
        """Should skip event by type."""
        cfg = {'ignore_event_types': ['fromGmail']}
        event = {'summary': 'Flight confirmation', 'eventType': 'fromGmail'}

        assert should_skip_event(event, cfg, []) is True

    def test_no_skip_different_event_type(self):
        """Should not skip when event type doesn't match."""
        cfg = {'ignore_event_types': ['fromGmail']}
        event = {'summary': 'Meeting', 'eventType': 'default'}

        assert should_skip_event(event, cfg, []) is False

    def test_skip_by_known_prefix(self):
        """Should skip event with known calendar prefix."""
        cfg = {'skip_if_title_has_known_prefix': True}
        known_prefixes = ['[WORK] ', '[PERS] ']
        event = {'summary': '[WORK] Team standup'}

        assert should_skip_event(event, cfg, known_prefixes) is True

    def test_no_skip_without_prefix(self):
        """Should not skip when title doesn't have known prefix."""
        cfg = {'skip_if_title_has_known_prefix': True}
        known_prefixes = ['[WORK] ', '[PERS] ']
        event = {'summary': 'Team standup'}

        assert should_skip_event(event, cfg, known_prefixes) is False

    def test_prefix_skip_can_be_disabled(self):
        """Prefix skip should respect configuration."""
        cfg = {'skip_if_title_has_known_prefix': False}
        known_prefixes = ['[WORK] ']
        event = {'summary': '[WORK] Meeting'}

        assert should_skip_event(event, cfg, known_prefixes) is False

    def test_empty_summary(self):
        """Should handle events with empty summary."""
        cfg = {'ignore_if_summary_contains': ['test']}
        event = {'summary': ''}

        assert should_skip_event(event, cfg, []) is False

    def test_none_summary(self):
        """Should handle events with None summary."""
        cfg = {'ignore_if_summary_contains': ['test']}
        event = {'summary': None}

        assert should_skip_event(event, cfg, []) is False

    def test_empty_config(self):
        """Should not skip with empty config."""
        event = {'summary': 'Any event'}

        assert should_skip_event(event, {}, []) is False

    def test_multiple_skip_conditions(self):
        """First matching condition should trigger skip."""
        cfg = {
            'ignore_if_summary_contains': ['birthday'],
            'ignore_event_types': ['fromGmail'],
        }
        event = {'summary': 'Birthday reminder', 'eventType': 'default'}

        assert should_skip_event(event, cfg, []) is True


class TestDesiredCopyVisibility:
    """Tests for desired_copy_visibility() function."""

    @pytest.fixture
    def calendar(self):
        """Create a test calendar."""
        return Calendar(
            name='TEST',
            calendar_id='primary',
            credentials_file='creds/test.json',
            token_file='tokens/test.json',
        )

    def test_default_visibility(self, calendar):
        """Should return 'private' as default."""
        result = desired_copy_visibility(calendar, {})
        assert result == 'private'

    def test_global_config_visibility(self, calendar):
        """Should use global config visibility."""
        cfg = {'default_copy_visibility': 'public'}

        result = desired_copy_visibility(calendar, cfg)

        assert result == 'public'

    def test_calendar_specific_visibility(self, calendar):
        """Calendar-specific should override global."""
        calendar.copy_visibility = 'confidential'
        cfg = {'default_copy_visibility': 'public'}

        result = desired_copy_visibility(calendar, cfg)

        assert result == 'confidential'

    def test_invalid_calendar_visibility_falls_back(self, calendar):
        """Invalid calendar visibility should fall back to global."""
        calendar.copy_visibility = 'invalid'
        cfg = {'default_copy_visibility': 'public'}

        result = desired_copy_visibility(calendar, cfg)

        assert result == 'public'

    def test_all_valid_visibilities(self, calendar):
        """Should accept all valid visibility values."""
        for vis in VALID_VISIBILITIES:
            calendar.copy_visibility = vis
            result = desired_copy_visibility(calendar, {})
            assert result == vis


class TestSyncContext:
    """Tests for SyncContext dataclass."""

    def test_create_context(self):
        """Should create context with config."""
        cfg = {'key': 'value'}
        ctx = SyncContext(config=cfg)

        assert ctx.config == cfg
        assert ctx.calendars == {}
        assert ctx.known_prefixes == []
        assert ctx.dry_run is False

    def test_get_calendar(self):
        """Should get calendar by name."""
        ctx = SyncContext(config={})
        cal = Calendar(
            name='WORK',
            calendar_id='primary',
            credentials_file='creds/work.json',
            token_file='tokens/work.json',
        )
        ctx.calendars['WORK'] = cal

        assert ctx.get_calendar('WORK') == cal
        assert ctx.get_calendar('NONEXISTENT') is None

    def test_get_all_calendars(self):
        """Should get all calendars."""
        ctx = SyncContext(config={})
        cal1 = Calendar(
            name='WORK',
            calendar_id='primary',
            credentials_file='creds/work.json',
            token_file='tokens/work.json',
        )
        cal2 = Calendar(
            name='PERS',
            calendar_id='primary',
            credentials_file='creds/pers.json',
            token_file='tokens/pers.json',
        )
        ctx.calendars['WORK'] = cal1
        ctx.calendars['PERS'] = cal2

        all_cals = ctx.get_all_calendars()

        assert len(all_cals) == 2
        assert cal1 in all_cals
        assert cal2 in all_cals

    def test_dry_run_mode(self):
        """Should support dry-run mode."""
        ctx = SyncContext(config={}, dry_run=True)
        assert ctx.dry_run is True


class TestCalendar:
    """Tests for Calendar dataclass."""

    def test_create_calendar(self):
        """Should create calendar with required fields."""
        cal = Calendar(
            name='WORK',
            calendar_id='primary',
            credentials_file='creds/work.json',
            token_file='tokens/work.json',
        )

        assert cal.name == 'WORK'
        assert cal.calendar_id == 'primary'
        assert cal.service is None
        assert cal.copy_visibility is None

    def test_calendar_with_service(self):
        """Should accept service object."""
        mock_service = Mock()
        cal = Calendar(
            name='WORK',
            calendar_id='primary',
            credentials_file='creds/work.json',
            token_file='tokens/work.json',
            service=mock_service,
        )

        assert cal.service == mock_service

    def test_calendar_config(self):
        """Should return calendar config dict."""
        cal = Calendar(
            name='WORK',
            calendar_id='primary',
            credentials_file='creds/work.json',
            token_file='tokens/work.json',
            copy_visibility='private',
        )

        config = cal.get_calendar_config()

        assert config['name'] == 'WORK'
        assert config['calendar_id'] == 'primary'
        assert config['copy_visibility'] == 'private'
