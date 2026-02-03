"""
Command-line interface for gcal_trisync.

This module provides the main entry point and argument parsing.
"""

from __future__ import annotations

import argparse
import logging
import sys

from .api import ensure_dirs, get_service
from .config import ConfigValidationError, load_config
from .models import Calendar, SyncContext
from .storage import StateStorage
from .sync import run_sync, run_sync_incremental

logger = logging.getLogger(__name__)


def setup_logging(verbose: bool = False) -> None:
    """
    Configure logging for the application.

    Args:
        verbose: If True, set log level to DEBUG
    """
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    """
    Parse command-line arguments.

    Args:
        args: Arguments to parse (defaults to sys.argv)

    Returns:
        Parsed arguments namespace
    """
    parser = argparse.ArgumentParser(
        prog='gcal_trisync',
        description="Bidirectional sync for Google Calendars with safe delete."
    )
    parser.add_argument(
        '--config',
        required=True,
        help='Path to config.yaml or config.json'
    )
    parser.add_argument(
        '--auth',
        choices=['local', 'console'],
        default='local',
        help='Authentication method (default: local)'
    )
    parser.add_argument(
        '--login-hint',
        dest='login_hint',
        default=None,
        help='Email to pre-fill in login'
    )
    parser.add_argument(
        '--port',
        type=int,
        default=0,
        help='Port for local OAuth server (default: auto-select)'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Simulate sync without making changes'
    )
    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Enable verbose logging'
    )
    parser.add_argument(
        '--version',
        action='version',
        version='%(prog)s 0.3.0'
    )

    # Incremental sync options
    parser.add_argument(
        '--incremental',
        action='store_true',
        help='Use incremental sync with sync tokens (faster)'
    )
    parser.add_argument(
        '--full-sync',
        action='store_true',
        dest='force_full',
        help='Force full sync even if sync tokens exist'
    )
    parser.add_argument(
        '--state-file',
        dest='state_file',
        default='.trisync_state.json',
        help='Path to state file for sync tokens (default: .trisync_state.json)'
    )
    parser.add_argument(
        '--clear-state',
        action='store_true',
        dest='clear_state',
        help='Clear saved state and exit'
    )

    return parser.parse_args(args)


def initialize_calendars(
    cfg: dict,
    auth_method: str,
    login_hint: str | None,
    port: int
) -> dict[str, Calendar]:
    """
    Initialize calendar services from configuration.

    Args:
        cfg: Configuration dictionary
        auth_method: Authentication method ('local' or 'console')
        login_hint: Email to pre-fill in login
        port: Port for local OAuth server

    Returns:
        Dictionary mapping calendar names to Calendar objects
    """
    calendars: dict[str, Calendar] = {}

    for c in cfg['calendars']:
        svc = get_service(
            c['credentials_file'],
            c['token_file'],
            auth_method=auth_method,
            login_hint=login_hint,
            port=port
        )

        cal = Calendar(
            name=c['name'],
            calendar_id=c['calendar_id'],
            credentials_file=c['credentials_file'],
            token_file=c['token_file'],
            service=svc,
            copy_visibility=c.get('copy_visibility')
        )
        calendars[c['name']] = cal

    return calendars


def main(args: list[str] | None = None) -> int:
    """
    Main entry point for gcal_trisync.

    Args:
        args: Command-line arguments (defaults to sys.argv)

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    parsed = parse_args(args)

    setup_logging(parsed.verbose)

    # Handle --clear-state
    if parsed.clear_state:
        storage = StateStorage(parsed.state_file)
        storage.clear()
        logger.info(f"State cleared from {parsed.state_file}")
        return 0

    if parsed.dry_run:
        logger.info("Running in DRY-RUN mode - no changes will be made")

    if parsed.incremental:
        logger.info("Using incremental sync mode")

    # Load and validate configuration
    try:
        cfg = load_config(parsed.config)
    except ConfigValidationError as e:
        logger.error(f"Configuration error: {e}")
        return 1
    except FileNotFoundError:
        logger.error(f"Configuration file not found: {parsed.config}")
        return 1

    ensure_dirs()

    # Initialize calendars
    try:
        calendars = initialize_calendars(
            cfg,
            parsed.auth,
            parsed.login_hint,
            parsed.port
        )
    except Exception as e:
        logger.error(f"Failed to initialize calendars: {e}")
        return 1

    # Create sync context
    ctx = SyncContext(config=cfg, dry_run=parsed.dry_run)
    for name, cal in calendars.items():
        ctx.add_calendar(cal)

    # Run sync
    try:
        if parsed.incremental:
            storage = StateStorage(parsed.state_file)
            run_sync_incremental(
                ctx,
                storage=storage,
                force_full=parsed.force_full
            )
        else:
            run_sync(ctx)
    except Exception as e:
        logger.error(f"Sync failed: {e}")
        return 1

    logger.info("Done.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
