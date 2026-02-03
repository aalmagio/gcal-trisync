"""
gcal_trisync — Bidirectional sync for Google Calendars

This package provides tools for synchronizing events across multiple
Google Calendar accounts with features like:

- Bidirectional sync across 2-3 calendars
- Origin prefixes in event titles
- Metadata-based loop prevention
- Safe deletion of orphaned events
- Event filtering by keywords and types
- Configurable visibility for copies

Usage:
    python -m gcal_trisync --config config.yaml

Or as a library:
    from gcal_trisync import SyncContext, Calendar, run_sync
"""

__version__ = '0.3.0'
__author__ = 'gcal-trisync contributors'

from .models import Calendar, SyncContext, VALID_VISIBILITIES
from .config import (
    ConfigValidationError,
    load_config,
    validate_config,
)
from .utils import (
    add_sync_note,
    canonical_event_dict,
    compute_chain_id,
    get_private_meta,
    get_time_window,
    iso,
    set_private_meta,
    title_with_origin,
)
from .sync import (
    create_copy_with_visibility,
    desired_copy_visibility,
    perform_safe_delete,
    process_chains,
    process_unsynced_events,
    run_sync,
    run_sync_incremental,
    should_skip_event,
    update_if_diff,
)
from .api import (
    SyncResult,
    create_event,
    delete_event,
    ensure_dirs,
    find_event_by_chain,
    get_event,
    get_service,
    list_events,
    list_events_full_sync,
    list_events_incremental,
    patch_event,
    sync_events,
    update_event,
)
from .storage import (
    CalendarState,
    StateStorage,
    SyncState,
)
from .retry import (
    RateLimiter,
    RateLimitError,
    RetryableError,
    default_rate_limiter,
    is_retryable_error,
    rate_limited,
    robust_api_call,
    with_retry,
)
from .metrics import (
    CalendarMetrics,
    MetricsCollector,
    SyncMetrics,
)
from .cli import main

__all__ = [
    # Version
    '__version__',

    # Models
    'Calendar',
    'SyncContext',
    'VALID_VISIBILITIES',

    # Config
    'ConfigValidationError',
    'load_config',
    'validate_config',

    # Utils
    'add_sync_note',
    'canonical_event_dict',
    'compute_chain_id',
    'get_private_meta',
    'get_time_window',
    'iso',
    'set_private_meta',
    'title_with_origin',

    # Sync
    'create_copy_with_visibility',
    'desired_copy_visibility',
    'perform_safe_delete',
    'process_chains',
    'process_unsynced_events',
    'run_sync',
    'run_sync_incremental',
    'should_skip_event',
    'update_if_diff',

    # API
    'SyncResult',
    'create_event',
    'delete_event',
    'ensure_dirs',
    'find_event_by_chain',
    'get_event',
    'get_service',
    'list_events',
    'list_events_full_sync',
    'list_events_incremental',
    'patch_event',
    'sync_events',
    'update_event',

    # Storage
    'CalendarState',
    'StateStorage',
    'SyncState',

    # Retry & Rate Limiting
    'RateLimiter',
    'RateLimitError',
    'RetryableError',
    'default_rate_limiter',
    'is_retryable_error',
    'rate_limited',
    'robust_api_call',
    'with_retry',

    # Metrics
    'CalendarMetrics',
    'MetricsCollector',
    'SyncMetrics',

    # CLI
    'main',
]
