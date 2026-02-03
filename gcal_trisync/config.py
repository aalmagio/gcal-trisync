"""
Configuration management for gcal_trisync.

This module handles loading, validation, and access to configuration.
"""

from __future__ import annotations

import json
import os
from typing import Any

import yaml

from .models import VALID_VISIBILITIES


class ConfigValidationError(Exception):
    """Raised when configuration validation fails."""
    pass


def validate_config(cfg: dict[str, Any], check_files: bool = True) -> None:
    """
    Validate configuration structure and values.

    Args:
        cfg: Configuration dictionary to validate
        check_files: Whether to check if credential files exist

    Raises:
        ConfigValidationError: If configuration is invalid
    """
    if 'calendars' not in cfg:
        raise ConfigValidationError("Configuration must contain 'calendars' section")

    if not isinstance(cfg['calendars'], list) or len(cfg['calendars']) < 2:
        raise ConfigValidationError("At least 2 calendars must be configured")

    required_cal_fields = {'name', 'calendar_id', 'credentials_file', 'token_file'}
    seen_names: set[str] = set()

    for i, cal in enumerate(cfg['calendars']):
        missing = required_cal_fields - set(cal.keys())
        if missing:
            raise ConfigValidationError(
                f"Calendar {i+1} missing required fields: {missing}"
            )

        if cal['name'] in seen_names:
            raise ConfigValidationError(
                f"Duplicate calendar name: {cal['name']}"
            )
        seen_names.add(cal['name'])

        if check_files and not os.path.exists(cal['credentials_file']):
            raise ConfigValidationError(
                f"Credentials file not found: {cal['credentials_file']}"
            )

    # Validate optional fields
    if 'default_copy_visibility' in cfg:
        vis = cfg['default_copy_visibility']
        if vis not in VALID_VISIBILITIES:
            raise ConfigValidationError(
                f"Invalid default_copy_visibility: {vis}. "
                f"Must be one of: {VALID_VISIBILITIES}"
            )

    # Validate numeric fields
    for field_name in ('window_days_past', 'window_days_future'):
        if field_name in cfg:
            try:
                val = int(cfg[field_name])
                if val < 0:
                    raise ValueError("Must be non-negative")
            except (ValueError, TypeError) as e:
                raise ConfigValidationError(
                    f"Invalid {field_name}: {cfg[field_name]} - {e}"
                )


def load_config(path: str, validate: bool = True) -> dict[str, Any]:
    """
    Load configuration from YAML or JSON file.

    Args:
        path: Path to configuration file
        validate: Whether to validate the configuration

    Returns:
        Configuration dictionary

    Raises:
        ConfigValidationError: If configuration is invalid
        FileNotFoundError: If file doesn't exist
    """
    with open(path, 'r', encoding='utf-8') as f:
        if path.endswith(('.yaml', '.yml')):
            cfg = yaml.safe_load(f)
        else:
            cfg = json.load(f)

    if validate:
        validate_config(cfg)

    return cfg


def get_config_value(
    cfg: dict[str, Any],
    key: str,
    default: Any = None
) -> Any:
    """
    Get a configuration value with optional default.

    Args:
        cfg: Configuration dictionary
        key: Key to look up
        default: Default value if key not found

    Returns:
        Configuration value or default
    """
    return cfg.get(key, default)
