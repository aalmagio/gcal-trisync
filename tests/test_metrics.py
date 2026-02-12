"""Tests for metrics and monitoring in gcal_trisync."""

import json
import os
import sys
import tempfile
import time
from importlib.util import spec_from_file_location, module_from_spec
from pathlib import Path

import pytest

# Import metrics module directly to avoid Google API dependencies
_metrics_path = Path(__file__).parent.parent / 'gcal_trisync' / 'metrics.py'
_spec = spec_from_file_location('gcal_trisync.metrics', _metrics_path)
_metrics = module_from_spec(_spec)
sys.modules['gcal_trisync.metrics'] = _metrics
_spec.loader.exec_module(_metrics)

CalendarMetrics = _metrics.CalendarMetrics
SyncMetrics = _metrics.SyncMetrics
MetricsCollector = _metrics.MetricsCollector


# ── CalendarMetrics Tests ───────────────────────────────────────────

class TestCalendarMetrics:
    """Tests for CalendarMetrics dataclass."""

    def test_create_default(self):
        """Should create with zero counters."""
        cm = CalendarMetrics(name='WORK')
        assert cm.name == 'WORK'
        assert cm.events_fetched == 0
        assert cm.events_created == 0
        assert cm.events_updated == 0
        assert cm.events_deleted == 0
        assert cm.events_skipped == 0
        assert cm.errors == 0
        assert cm.sync_type == ''
        assert cm.fetch_duration_s == 0.0

    def test_to_dict(self):
        """Should serialize to dictionary."""
        cm = CalendarMetrics(
            name='PERS',
            events_fetched=50,
            events_created=3,
            events_updated=2,
            events_deleted=1,
            events_skipped=5,
            errors=0,
            sync_type='incremental',
            fetch_duration_s=1.234
        )
        d = cm.to_dict()
        assert d['name'] == 'PERS'
        assert d['events_fetched'] == 50
        assert d['events_created'] == 3
        assert d['events_updated'] == 2
        assert d['events_deleted'] == 1
        assert d['events_skipped'] == 5
        assert d['errors'] == 0
        assert d['sync_type'] == 'incremental'
        assert d['fetch_duration_s'] == 1.234

    def test_to_dict_rounds_duration(self):
        """Should round fetch_duration_s to 3 decimals."""
        cm = CalendarMetrics(name='X', fetch_duration_s=1.23456789)
        d = cm.to_dict()
        assert d['fetch_duration_s'] == 1.235


# ── SyncMetrics Tests ───────────────────────────────────────────────

class TestSyncMetrics:
    """Tests for SyncMetrics dataclass."""

    def test_create_default(self):
        """Should create with zero values."""
        sm = SyncMetrics()
        assert sm.start_time is None
        assert sm.end_time is None
        assert sm.duration_s == 0.0
        assert sm.calendars == {}
        assert sm.total_created == 0
        assert sm.total_updated == 0
        assert sm.total_deleted == 0
        assert sm.total_errors == 0
        assert sm.chains_processed == 0
        assert sm.dry_run is False
        assert sm.incremental is False

    def test_to_dict(self):
        """Should serialize to complete dictionary."""
        sm = SyncMetrics(
            start_time='2025-01-01T00:00:00Z',
            end_time='2025-01-01T00:01:00Z',
            duration_s=60.123,
            dry_run=True,
            incremental=True,
            total_created=5,
            total_updated=3,
            chains_processed=10,
        )
        d = sm.to_dict()
        assert d['start_time'] == '2025-01-01T00:00:00Z'
        assert d['end_time'] == '2025-01-01T00:01:00Z'
        assert d['duration_s'] == 60.123
        assert d['dry_run'] is True
        assert d['incremental'] is True
        assert d['totals']['created'] == 5
        assert d['totals']['updated'] == 3
        assert d['totals']['chains_processed'] == 10

    def test_to_dict_includes_calendars(self):
        """Should include per-calendar metrics in dict."""
        sm = SyncMetrics()
        sm.calendars['WORK'] = CalendarMetrics(name='WORK', events_fetched=10)
        d = sm.to_dict()
        assert 'WORK' in d['calendars']
        assert d['calendars']['WORK']['events_fetched'] == 10

    def test_to_json(self):
        """Should produce valid JSON."""
        sm = SyncMetrics(
            start_time='2025-01-01T00:00:00Z',
            total_created=5,
        )
        j = sm.to_json()
        parsed = json.loads(j)
        assert parsed['totals']['created'] == 5


# ── MetricsCollector Tests ──────────────────────────────────────────

class TestMetricsCollector:
    """Tests for MetricsCollector."""

    def test_create_default(self):
        """Should create with default settings."""
        mc = MetricsCollector()
        assert mc.metrics.dry_run is False
        assert mc.metrics.incremental is False

    def test_create_with_flags(self):
        """Should accept dry_run and incremental flags."""
        mc = MetricsCollector(dry_run=True, incremental=True)
        assert mc.metrics.dry_run is True
        assert mc.metrics.incremental is True

    def test_context_manager(self):
        """Should work as context manager and set timing."""
        with MetricsCollector() as mc:
            time.sleep(0.01)
        assert mc.metrics.start_time is not None
        assert mc.metrics.end_time is not None
        assert mc.metrics.duration_s > 0

    def test_start_stop(self):
        """Should record timing on start/stop."""
        mc = MetricsCollector()
        mc.start()
        time.sleep(0.01)
        mc.stop()
        assert mc.metrics.start_time is not None
        assert mc.metrics.end_time is not None
        assert mc.metrics.duration_s >= 0.01

    def test_record_fetch(self):
        """Should record fetch count and type."""
        mc = MetricsCollector()
        mc.record_fetch('WORK', 150, sync_type='full')
        cm = mc.metrics.calendars['WORK']
        assert cm.events_fetched == 150
        assert cm.sync_type == 'full'

    def test_record_fetch_with_timer(self):
        """Should record fetch duration when timer started."""
        mc = MetricsCollector()
        mc.start_fetch('WORK')
        time.sleep(0.02)
        mc.record_fetch('WORK', 100, sync_type='incremental')
        cm = mc.metrics.calendars['WORK']
        assert cm.fetch_duration_s >= 0.02
        assert cm.sync_type == 'incremental'

    def test_record_create(self):
        """Should increment create counter."""
        mc = MetricsCollector()
        mc.record_create('PERS')
        mc.record_create('PERS')
        mc.record_create('WORK')
        assert mc.metrics.calendars['PERS'].events_created == 2
        assert mc.metrics.calendars['WORK'].events_created == 1

    def test_record_update(self):
        """Should increment update counter."""
        mc = MetricsCollector()
        mc.record_update('WORK')
        assert mc.metrics.calendars['WORK'].events_updated == 1

    def test_record_delete(self):
        """Should increment delete counter."""
        mc = MetricsCollector()
        mc.record_delete('PERS')
        assert mc.metrics.calendars['PERS'].events_deleted == 1

    def test_record_skip(self):
        """Should increment skip counter."""
        mc = MetricsCollector()
        mc.record_skip('WORK')
        mc.record_skip('WORK')
        assert mc.metrics.calendars['WORK'].events_skipped == 2

    def test_record_error(self):
        """Should increment error counter."""
        mc = MetricsCollector()
        mc.record_error('WORK')
        assert mc.metrics.calendars['WORK'].errors == 1

    def test_record_chains(self):
        """Should record chains processed count."""
        mc = MetricsCollector()
        mc.record_chains(42)
        assert mc.metrics.chains_processed == 42

    def test_record_api_call(self):
        """Should increment API call counter."""
        mc = MetricsCollector()
        mc.record_api_call()
        mc.record_api_call()
        assert mc.metrics.api_calls == 2

    def test_record_api_retry(self):
        """Should increment API retry counter."""
        mc = MetricsCollector()
        mc.record_api_retry()
        assert mc.metrics.api_retries == 1

    def test_compute_totals(self):
        """Should aggregate per-calendar metrics into totals."""
        mc = MetricsCollector()
        mc.record_fetch('WORK', 100)
        mc.record_fetch('PERS', 50)
        mc.record_create('WORK')
        mc.record_create('PERS')
        mc.record_create('PERS')
        mc.record_update('WORK')
        mc.record_delete('WORK')
        mc.record_skip('PERS')
        mc.record_error('WORK')

        mc._compute_totals()
        m = mc.metrics
        assert m.total_events_fetched == 150
        assert m.total_created == 3
        assert m.total_updated == 1
        assert m.total_deleted == 1
        assert m.total_skipped == 1
        assert m.total_errors == 1

    def test_multiple_calendars(self):
        """Should track metrics independently per calendar."""
        mc = MetricsCollector()
        mc.record_fetch('A', 10, sync_type='full')
        mc.record_fetch('B', 20, sync_type='incremental')
        mc.record_fetch('C', 30, sync_type='full')

        assert mc.metrics.calendars['A'].events_fetched == 10
        assert mc.metrics.calendars['B'].events_fetched == 20
        assert mc.metrics.calendars['C'].events_fetched == 30
        assert mc.metrics.calendars['B'].sync_type == 'incremental'


# ── Report Tests ────────────────────────────────────────────────────

class TestReport:
    """Tests for report generation."""

    def test_report_contains_header(self):
        """Should include TRISYNC header."""
        mc = MetricsCollector()
        mc.start()
        mc.stop()
        report = mc.report()
        assert 'TRISYNC' in report
        assert 'Sync Report' in report

    def test_report_shows_mode(self):
        """Should show DRY-RUN mode."""
        mc = MetricsCollector(dry_run=True)
        mc.start()
        mc.stop()
        report = mc.report()
        assert 'DRY-RUN' in report

    def test_report_shows_live_mode(self):
        """Should show LIVE mode when not dry-run."""
        mc = MetricsCollector(dry_run=False)
        mc.start()
        mc.stop()
        report = mc.report()
        assert 'LIVE' in report

    def test_report_shows_sync_type(self):
        """Should show incremental sync type."""
        mc = MetricsCollector(incremental=True)
        mc.start()
        mc.stop()
        report = mc.report()
        assert 'Incremental' in report

    def test_report_shows_full_sync_type(self):
        """Should show Full sync type."""
        mc = MetricsCollector(incremental=False)
        mc.start()
        mc.stop()
        report = mc.report()
        assert 'Full' in report

    def test_report_shows_calendar_data(self):
        """Should include per-calendar metrics."""
        mc = MetricsCollector()
        mc.start()
        mc.record_fetch('WORK', 100, sync_type='full')
        mc.record_create('WORK')
        mc.stop()
        report = mc.report()
        assert '[WORK]' in report
        assert '100' in report

    def test_report_shows_totals(self):
        """Should include totals section."""
        mc = MetricsCollector()
        mc.start()
        mc.record_fetch('A', 50)
        mc.record_fetch('B', 30)
        mc.record_create('A')
        mc.stop()
        report = mc.report()
        assert 'Totals' in report

    def test_report_shows_errors(self):
        """Should include error count when non-zero."""
        mc = MetricsCollector()
        mc.start()
        mc.record_error('WORK')
        mc.stop()
        report = mc.report()
        assert 'Errors' in report

    def test_report_shows_duration(self):
        """Should include duration."""
        mc = MetricsCollector()
        mc.start()
        mc.stop()
        report = mc.report()
        assert 'Duration' in report


# ── JSON Export Tests ───────────────────────────────────────────────

class TestJsonExport:
    """Tests for JSON metrics export."""

    def test_save_json(self):
        """Should save valid JSON file."""
        mc = MetricsCollector()
        mc.start()
        mc.record_fetch('WORK', 100, sync_type='full')
        mc.record_create('WORK')
        mc.record_create('WORK')
        mc.stop()

        with tempfile.NamedTemporaryFile(
            mode='w', suffix='.json', delete=False
        ) as f:
            filepath = f.name

        try:
            mc.save_json(filepath)
            with open(filepath, encoding='utf-8') as f:
                data = json.load(f)

            assert data['totals']['created'] == 2
            assert data['totals']['events_fetched'] == 100
            assert 'WORK' in data['calendars']
            assert data['calendars']['WORK']['events_created'] == 2
            assert data['start_time'] is not None
            assert data['duration_s'] >= 0
        finally:
            os.unlink(filepath)

    def test_to_json_roundtrip(self):
        """Should produce JSON that can be parsed back."""
        mc = MetricsCollector(dry_run=True, incremental=True)
        mc.start()
        mc.record_fetch('PERS', 25, sync_type='incremental')
        mc.record_update('PERS')
        mc.record_chains(5)
        mc.stop()

        j = mc.metrics.to_json()
        parsed = json.loads(j)
        assert parsed['dry_run'] is True
        assert parsed['incremental'] is True
        assert parsed['totals']['updated'] == 1
        assert parsed['totals']['chains_processed'] == 5
        assert parsed['calendars']['PERS']['sync_type'] == 'incremental'


# ── Edge Cases ──────────────────────────────────────────────────────

class TestEdgeCases:
    """Tests for edge cases."""

    def test_report_without_start(self):
        """Should generate report even if start/stop not called."""
        mc = MetricsCollector()
        report = mc.report()
        assert 'TRISYNC' in report

    def test_empty_metrics_report(self):
        """Should generate report with no data."""
        mc = MetricsCollector()
        mc.start()
        mc.stop()
        report = mc.report()
        assert 'Events fetched:   0' in report
        assert 'Events created:   0' in report

    def test_record_before_start(self):
        """Should record metrics even before start()."""
        mc = MetricsCollector()
        mc.record_create('WORK')
        assert mc.metrics.calendars['WORK'].events_created == 1

    def test_stop_computes_totals(self):
        """Should compute totals on stop."""
        mc = MetricsCollector()
        mc.start()
        mc.record_fetch('A', 10)
        mc.record_fetch('B', 20)
        mc.stop()
        assert mc.metrics.total_events_fetched == 30
