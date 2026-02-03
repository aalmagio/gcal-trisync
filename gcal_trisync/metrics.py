"""
Metrics and monitoring for gcal_trisync.

Collects statistics during sync operations and generates reports.
Supports JSON export for integration with external monitoring tools.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class CalendarMetrics:
    """
    Per-calendar sync metrics.

    Attributes:
        name: Calendar name
        events_fetched: Number of events fetched
        events_created: Events created in this calendar
        events_updated: Events updated in this calendar
        events_deleted: Events deleted from this calendar
        events_skipped: Events skipped during processing
        errors: Number of errors for this calendar
        sync_type: 'full' or 'incremental'
        fetch_duration_s: Time to fetch events (seconds)
    """
    name: str
    events_fetched: int = 0
    events_created: int = 0
    events_updated: int = 0
    events_deleted: int = 0
    events_skipped: int = 0
    errors: int = 0
    sync_type: str = ''
    fetch_duration_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            'name': self.name,
            'events_fetched': self.events_fetched,
            'events_created': self.events_created,
            'events_updated': self.events_updated,
            'events_deleted': self.events_deleted,
            'events_skipped': self.events_skipped,
            'errors': self.errors,
            'sync_type': self.sync_type,
            'fetch_duration_s': round(self.fetch_duration_s, 3),
        }


@dataclass
class SyncMetrics:
    """
    Aggregate metrics for a complete sync run.

    Attributes:
        start_time: When the sync started (UTC)
        end_time: When the sync ended (UTC)
        duration_s: Total sync duration in seconds
        calendars: Per-calendar metrics
        total_events_fetched: Total events fetched across all calendars
        total_created: Total events created
        total_updated: Total events updated
        total_deleted: Total events deleted
        total_skipped: Total events skipped
        total_errors: Total errors
        chains_processed: Number of event chains processed
        api_calls: Total API calls made
        api_retries: Total retry attempts
        dry_run: Whether this was a dry run
        incremental: Whether incremental sync was used
    """
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    duration_s: float = 0.0
    calendars: dict[str, CalendarMetrics] = field(default_factory=dict)
    total_events_fetched: int = 0
    total_created: int = 0
    total_updated: int = 0
    total_deleted: int = 0
    total_skipped: int = 0
    total_errors: int = 0
    chains_processed: int = 0
    api_calls: int = 0
    api_retries: int = 0
    dry_run: bool = False
    incremental: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            'start_time': self.start_time,
            'end_time': self.end_time,
            'duration_s': round(self.duration_s, 3),
            'dry_run': self.dry_run,
            'incremental': self.incremental,
            'totals': {
                'events_fetched': self.total_events_fetched,
                'created': self.total_created,
                'updated': self.total_updated,
                'deleted': self.total_deleted,
                'skipped': self.total_skipped,
                'errors': self.total_errors,
                'chains_processed': self.chains_processed,
                'api_calls': self.api_calls,
                'api_retries': self.api_retries,
            },
            'calendars': {
                name: cm.to_dict()
                for name, cm in self.calendars.items()
            },
        }

    def to_json(self, indent: int = 2) -> str:
        """Serialize metrics to JSON string."""
        return json.dumps(self.to_dict(), indent=indent)


class MetricsCollector:
    """
    Collects metrics during a sync run.

    Usage as a context manager:
        with MetricsCollector() as mc:
            mc.record_fetch('WORK', 150, sync_type='incremental')
            mc.record_create('PERS')
            ...
        report = mc.report()
    """

    def __init__(self, dry_run: bool = False, incremental: bool = False) -> None:
        self._metrics = SyncMetrics(dry_run=dry_run, incremental=incremental)
        self._start_mono: float = 0.0
        self._fetch_timers: dict[str, float] = {}

    @property
    def metrics(self) -> SyncMetrics:
        """Access the collected metrics."""
        return self._metrics

    def __enter__(self) -> 'MetricsCollector':
        self.start()
        return self

    def __exit__(self, *args: Any) -> None:
        self.stop()

    def start(self) -> None:
        """Mark the start of a sync run."""
        self._start_mono = time.monotonic()
        self._metrics.start_time = datetime.now(timezone.utc).isoformat()

    def stop(self) -> None:
        """Mark the end of a sync run and finalize metrics."""
        self._metrics.end_time = datetime.now(timezone.utc).isoformat()
        self._metrics.duration_s = time.monotonic() - self._start_mono
        self._compute_totals()

    def _ensure_calendar(self, calendar_name: str) -> CalendarMetrics:
        """Get or create CalendarMetrics for a calendar."""
        if calendar_name not in self._metrics.calendars:
            self._metrics.calendars[calendar_name] = CalendarMetrics(
                name=calendar_name
            )
        return self._metrics.calendars[calendar_name]

    def _compute_totals(self) -> None:
        """Recompute totals from per-calendar metrics."""
        m = self._metrics
        m.total_events_fetched = sum(
            c.events_fetched for c in m.calendars.values()
        )
        m.total_created = sum(c.events_created for c in m.calendars.values())
        m.total_updated = sum(c.events_updated for c in m.calendars.values())
        m.total_deleted = sum(c.events_deleted for c in m.calendars.values())
        m.total_skipped = sum(c.events_skipped for c in m.calendars.values())
        m.total_errors = sum(c.errors for c in m.calendars.values())

    # ── Recording methods ───────────────────────────────────────────

    def start_fetch(self, calendar_name: str) -> None:
        """Record the start of event fetching for a calendar."""
        self._fetch_timers[calendar_name] = time.monotonic()

    def record_fetch(
        self,
        calendar_name: str,
        event_count: int,
        sync_type: str = 'full'
    ) -> None:
        """
        Record events fetched for a calendar.

        Args:
            calendar_name: Calendar that was fetched
            event_count: Number of events retrieved
            sync_type: 'full' or 'incremental'
        """
        cm = self._ensure_calendar(calendar_name)
        cm.events_fetched = event_count
        cm.sync_type = sync_type
        if calendar_name in self._fetch_timers:
            cm.fetch_duration_s = (
                time.monotonic() - self._fetch_timers[calendar_name]
            )
            del self._fetch_timers[calendar_name]

    def record_create(self, calendar_name: str) -> None:
        """Record an event creation in a calendar."""
        self._ensure_calendar(calendar_name).events_created += 1

    def record_update(self, calendar_name: str) -> None:
        """Record an event update in a calendar."""
        self._ensure_calendar(calendar_name).events_updated += 1

    def record_delete(self, calendar_name: str) -> None:
        """Record an event deletion from a calendar."""
        self._ensure_calendar(calendar_name).events_deleted += 1

    def record_skip(self, calendar_name: str) -> None:
        """Record an event skipped during processing."""
        self._ensure_calendar(calendar_name).events_skipped += 1

    def record_error(self, calendar_name: str) -> None:
        """Record an error for a calendar."""
        self._ensure_calendar(calendar_name).errors += 1

    def record_chains(self, count: int) -> None:
        """Record number of chains processed."""
        self._metrics.chains_processed = count

    def record_api_call(self) -> None:
        """Record an API call."""
        self._metrics.api_calls += 1

    def record_api_retry(self) -> None:
        """Record an API retry attempt."""
        self._metrics.api_retries += 1

    # ── Report generation ───────────────────────────────────────────

    def report(self) -> str:
        """
        Generate a human-readable text report.

        Returns:
            Formatted report string
        """
        self._compute_totals()
        m = self._metrics
        lines: list[str] = []

        lines.append('=' * 60)
        lines.append('  TRISYNC - Sync Report')
        lines.append('=' * 60)
        lines.append('')

        # Run info
        mode = 'DRY-RUN' if m.dry_run else 'LIVE'
        sync_type = 'Incremental' if m.incremental else 'Full'
        lines.append(f'  Mode:       {mode}')
        lines.append(f'  Sync type:  {sync_type}')
        if m.start_time:
            lines.append(f'  Started:    {m.start_time}')
        lines.append(f'  Duration:   {m.duration_s:.2f}s')
        lines.append('')

        # Per-calendar summary
        lines.append('-' * 60)
        lines.append('  Per-Calendar Summary')
        lines.append('-' * 60)

        for name, cm in m.calendars.items():
            lines.append(f'')
            lines.append(f'  [{name}]')
            sync_label = f' ({cm.sync_type})' if cm.sync_type else ''
            lines.append(f'    Fetched:  {cm.events_fetched}{sync_label}')
            if cm.fetch_duration_s > 0:
                lines.append(f'    Fetch time: {cm.fetch_duration_s:.2f}s')
            lines.append(
                f'    Created: {cm.events_created}  '
                f'Updated: {cm.events_updated}  '
                f'Deleted: {cm.events_deleted}'
            )
            if cm.events_skipped:
                lines.append(f'    Skipped:  {cm.events_skipped}')
            if cm.errors:
                lines.append(f'    Errors:   {cm.errors}')

        lines.append('')

        # Totals
        lines.append('-' * 60)
        lines.append('  Totals')
        lines.append('-' * 60)
        lines.append(f'  Events fetched:   {m.total_events_fetched}')
        lines.append(f'  Events created:   {m.total_created}')
        lines.append(f'  Events updated:   {m.total_updated}')
        lines.append(f'  Events deleted:   {m.total_deleted}')
        if m.total_skipped:
            lines.append(f'  Events skipped:   {m.total_skipped}')
        lines.append(f'  Chains processed: {m.chains_processed}')
        if m.total_errors:
            lines.append(f'  Errors:           {m.total_errors}')
        if m.api_calls:
            lines.append(f'  API calls:        {m.api_calls}')
        if m.api_retries:
            lines.append(f'  API retries:      {m.api_retries}')

        lines.append('')
        lines.append('=' * 60)

        return '\n'.join(lines)

    def save_json(self, filepath: str) -> None:
        """
        Save metrics to a JSON file.

        Args:
            filepath: Path to write JSON metrics
        """
        self._compute_totals()
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(self._metrics.to_json())
        logger.info(f"Metrics saved to {filepath}")
