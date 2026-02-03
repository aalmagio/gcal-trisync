#!/usr/bin/env python3
"""
gcal_trisync.py — Bidirectional sync for Google Calendars

This is the backwards-compatible entry point. The actual implementation
is now in the gcal_trisync package.

Usage:
    python gcal_trisync.py --config config.yaml

Or use the package directly:
    python -m gcal_trisync --config config.yaml

License: MIT
"""

import sys

# Import everything from the package for backwards compatibility
from gcal_trisync import (
    # Version
    __version__,

    # Models
    Calendar,
    SyncContext,
    VALID_VISIBILITIES,

    # Config
    ConfigValidationError,
    load_config,
    validate_config,

    # Utils
    add_sync_note,
    canonical_event_dict,
    compute_chain_id,
    get_private_meta,
    get_time_window,
    iso,
    set_private_meta,
    title_with_origin,

    # Sync
    create_copy_with_visibility,
    desired_copy_visibility,
    perform_safe_delete,
    process_chains,
    process_unsynced_events,
    run_sync,
    should_skip_event,
    update_if_diff,

    # API
    create_event,
    delete_event,
    ensure_dirs,
    find_event_by_chain,
    get_event,
    get_service,
    list_events,
    patch_event,
    update_event,

    # CLI
    main,
)


if __name__ == '__main__':
    sys.exit(main())
