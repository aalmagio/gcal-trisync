"""Tests for utility functions in gcal_trisync."""

import pytest
from datetime import datetime, timezone

import sys
sys.path.insert(0, '.')

# Import from trisync_core (no Google API dependencies)
from trisync_core import (
    iso,
    compute_chain_id,
    canonical_event_dict,
    get_private_meta,
    set_private_meta,
    title_with_origin,
    add_sync_note,
    get_time_window,
)


class TestIso:
    """Tests for iso() function."""

    def test_iso_with_timezone(self):
        """ISO format with timezone should be preserved."""
        dt = datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
        result = iso(dt)
        assert "2024-01-15" in result
        assert "10:30:00" in result

    def test_iso_without_timezone(self):
        """Naive datetime should get UTC timezone."""
        dt = datetime(2024, 1, 15, 10, 30, 0)
        result = iso(dt)
        assert "+00:00" in result or "Z" in result

    def test_iso_returns_string(self):
        """Result should be a string."""
        dt = datetime.now(timezone.utc)
        assert isinstance(iso(dt), str)


class TestComputeChainId:
    """Tests for compute_chain_id() function."""

    def test_returns_hex_string(self):
        """Chain ID should be a hex string."""
        result = compute_chain_id("WORK", "event123")
        assert isinstance(result, str)
        assert all(c in '0123456789abcdef' for c in result)

    def test_sha256_length(self):
        """SHA-256 produces 64 character hex string."""
        result = compute_chain_id("WORK", "event123")
        assert len(result) == 64

    def test_deterministic(self):
        """Same inputs should produce same output."""
        result1 = compute_chain_id("WORK", "event123")
        result2 = compute_chain_id("WORK", "event123")
        assert result1 == result2

    def test_different_inputs_different_output(self):
        """Different inputs should produce different outputs."""
        result1 = compute_chain_id("WORK", "event123")
        result2 = compute_chain_id("PERS", "event123")
        result3 = compute_chain_id("WORK", "event456")
        assert result1 != result2
        assert result1 != result3
        assert result2 != result3

    def test_empty_strings(self):
        """Should handle empty strings."""
        result = compute_chain_id("", "")
        assert len(result) == 64


class TestCanonicalEventDict:
    """Tests for canonical_event_dict() function."""

    def test_extracts_correct_fields(self):
        """Should extract only canonical fields."""
        event = {
            'id': 'abc123',
            'summary': 'Meeting',
            'location': 'Office',
            'description': 'Weekly sync',
            'start': {'dateTime': '2024-01-15T10:00:00Z'},
            'end': {'dateTime': '2024-01-15T11:00:00Z'},
            'attendees': [{'email': 'test@example.com'}],
            'reminders': {'useDefault': True},
        }

        result = canonical_event_dict(event)

        assert 'summary' in result
        assert 'location' in result
        assert 'description' in result
        assert 'start' in result
        assert 'end' in result
        assert 'id' not in result
        assert 'attendees' not in result
        assert 'reminders' not in result

    def test_handles_missing_fields(self):
        """Should return empty values for missing fields."""
        event = {'id': 'abc123'}

        result = canonical_event_dict(event)

        assert result['summary'] == ''
        assert result['location'] == ''
        assert result['description'] == ''
        assert result['start'] == {}
        assert result['end'] == {}

    def test_empty_event(self):
        """Should handle empty event dict."""
        result = canonical_event_dict({})
        assert len(result) == 6

    def test_transparency_defaults_to_opaque(self):
        """Missing transparency means busy ('opaque') per Google API."""
        result = canonical_event_dict({'summary': 'Meeting'})
        assert result['transparency'] == 'opaque'

    def test_transparency_preserved(self):
        """Free ('transparent') events should keep their state."""
        result = canonical_event_dict({'transparency': 'transparent'})
        assert result['transparency'] == 'transparent'

    def test_busy_and_free_events_differ(self):
        """A busy and a free event must not compare as equal."""
        busy = canonical_event_dict({'summary': 'X'})
        free = canonical_event_dict({'summary': 'X', 'transparency': 'transparent'})
        assert busy != free


class TestPrivateMeta:
    """Tests for get_private_meta() and set_private_meta() functions."""

    def test_get_meta_from_event(self):
        """Should extract private metadata."""
        event = {
            'extendedProperties': {
                'private': {
                    'trisync': '1',
                    'trisync_chain_id': 'abc123'
                }
            }
        }

        result = get_private_meta(event)

        assert result['trisync'] == '1'
        assert result['trisync_chain_id'] == 'abc123'

    def test_get_meta_missing_properties(self):
        """Should return empty dict when no properties."""
        assert get_private_meta({}) == {}
        assert get_private_meta({'extendedProperties': {}}) == {}
        assert get_private_meta({'extendedProperties': {'private': None}}) == {}

    def test_set_meta_creates_structure(self):
        """Should create extendedProperties structure if missing."""
        event = {}

        set_private_meta(event, {'key': 'value'})

        assert event['extendedProperties']['private']['key'] == 'value'

    def test_set_meta_updates_existing(self):
        """Should update existing metadata."""
        event = {
            'extendedProperties': {
                'private': {'existing': 'data'}
            }
        }

        set_private_meta(event, {'new': 'value'})

        assert event['extendedProperties']['private']['existing'] == 'data'
        assert event['extendedProperties']['private']['new'] == 'value'

    def test_set_meta_overwrites(self):
        """Should overwrite existing keys."""
        event = {
            'extendedProperties': {
                'private': {'key': 'old'}
            }
        }

        set_private_meta(event, {'key': 'new'})

        assert event['extendedProperties']['private']['key'] == 'new'


class TestTitleWithOrigin:
    """Tests for title_with_origin() function."""

    def test_adds_prefix_when_enabled(self):
        """Should add prefix when enabled."""
        result = title_with_origin(True, "WORK", "Meeting")
        assert result == "[WORK] Meeting"

    def test_no_prefix_when_disabled(self):
        """Should not add prefix when disabled."""
        result = title_with_origin(False, "WORK", "Meeting")
        assert result == "Meeting"

    def test_no_duplicate_prefix(self):
        """Should not add duplicate prefix."""
        result = title_with_origin(True, "WORK", "[WORK] Meeting")
        assert result == "[WORK] Meeting"

    def test_handles_empty_title(self):
        """Should handle empty title."""
        assert title_with_origin(True, "WORK", "") == "[WORK] "
        assert title_with_origin(True, "WORK", None) == "[WORK] "
        assert title_with_origin(False, "WORK", None) == ""

    def test_handles_none_title(self):
        """Should handle None title."""
        result = title_with_origin(False, "WORK", None)
        assert result == ""


class TestAddSyncNote:
    """Tests for add_sync_note() function."""

    def test_adds_note_to_empty_description(self):
        """Should add note when description is empty."""
        result = add_sync_note("", "Synced by trisync")
        assert result == "Synced by trisync"

    def test_adds_note_to_existing_description(self):
        """Should append note to existing description."""
        result = add_sync_note("Original text", "Synced by trisync")
        assert "Original text" in result
        assert "Synced by trisync" in result
        assert "\n\n" in result

    def test_no_duplicate_note(self):
        """Should not add duplicate note."""
        desc = "Original text\n\nSynced by trisync"
        result = add_sync_note(desc, "Synced by trisync")
        assert result.count("Synced by trisync") == 1

    def test_handles_none_values(self):
        """Should handle None values."""
        assert add_sync_note(None, "Note") == "Note"
        assert add_sync_note("Text", None) == "Text"
        assert add_sync_note(None, None) == ""

    def test_empty_note(self):
        """Should return original when note is empty."""
        assert add_sync_note("Text", "") == "Text"


class TestGetTimeWindow:
    """Tests for get_time_window() function."""

    def test_returns_tuple_of_strings(self):
        """Should return tuple of ISO strings."""
        cfg = {'window_days_past': 30, 'window_days_future': 365}

        time_min, time_max = get_time_window(cfg)

        assert isinstance(time_min, str)
        assert isinstance(time_max, str)

    def test_time_min_is_before_time_max(self):
        """time_min should be before time_max."""
        cfg = {'window_days_past': 30, 'window_days_future': 365}

        time_min, time_max = get_time_window(cfg)

        assert time_min < time_max

    def test_uses_defaults(self):
        """Should use default values when not specified."""
        cfg = {}

        time_min, time_max = get_time_window(cfg)

        # Should not raise and return valid values
        assert time_min < time_max

    def test_custom_values(self):
        """Should respect custom window values."""
        cfg = {'window_days_past': 7, 'window_days_future': 14}

        time_min, time_max = get_time_window(cfg)

        # Values should be valid ISO strings
        assert 'T' in time_min
        assert 'T' in time_max
