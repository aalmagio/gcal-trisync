"""Tests for configuration loading and validation."""

import json
import os
import tempfile
from pathlib import Path

import pytest
import yaml

import sys
sys.path.insert(0, '.')

from trisync_core import (
    validate_config,
    ConfigValidationError,
)


def load_config(path: str) -> dict:
    """Load configuration from YAML or JSON file for testing."""
    with open(path, 'r', encoding='utf-8') as f:
        if path.endswith(('.yaml', '.yml')):
            cfg = yaml.safe_load(f)
        else:
            cfg = json.load(f)
    validate_config(cfg)
    return cfg


@pytest.fixture
def temp_creds_file():
    """Create a temporary credentials file for testing."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump({'installed': {'client_id': 'test'}}, f)
        yield f.name
    os.unlink(f.name)


@pytest.fixture
def valid_config(temp_creds_file):
    """Return a valid configuration dict."""
    return {
        'window_days_past': 30,
        'window_days_future': 365,
        'calendars': [
            {
                'name': 'WORK',
                'calendar_id': 'primary',
                'credentials_file': temp_creds_file,
                'token_file': 'tokens/work.json',
            },
            {
                'name': 'PERS',
                'calendar_id': 'primary',
                'credentials_file': temp_creds_file,
                'token_file': 'tokens/pers.json',
            },
        ],
    }


class TestValidateConfig:
    """Tests for validate_config() function."""

    def test_valid_config_passes(self, valid_config):
        """Valid configuration should not raise."""
        validate_config(valid_config)  # Should not raise

    def test_missing_calendars_section(self):
        """Should raise when calendars section is missing."""
        cfg = {'window_days_past': 30}

        with pytest.raises(ConfigValidationError, match="calendars"):
            validate_config(cfg)

    def test_less_than_two_calendars(self, temp_creds_file):
        """Should raise when less than 2 calendars."""
        cfg = {
            'calendars': [
                {
                    'name': 'WORK',
                    'calendar_id': 'primary',
                    'credentials_file': temp_creds_file,
                    'token_file': 'tokens/work.json',
                }
            ]
        }

        with pytest.raises(ConfigValidationError, match="2 calendars"):
            validate_config(cfg)

    def test_missing_calendar_fields(self, temp_creds_file):
        """Should raise when calendar is missing required fields."""
        cfg = {
            'calendars': [
                {'name': 'WORK'},  # Missing other fields
                {
                    'name': 'PERS',
                    'calendar_id': 'primary',
                    'credentials_file': temp_creds_file,
                    'token_file': 'tokens/pers.json',
                },
            ]
        }

        with pytest.raises(ConfigValidationError, match="missing required fields"):
            validate_config(cfg)

    def test_duplicate_calendar_names(self, temp_creds_file):
        """Should raise when calendar names are duplicated."""
        cfg = {
            'calendars': [
                {
                    'name': 'WORK',
                    'calendar_id': 'primary',
                    'credentials_file': temp_creds_file,
                    'token_file': 'tokens/work.json',
                },
                {
                    'name': 'WORK',  # Duplicate!
                    'calendar_id': 'secondary',
                    'credentials_file': temp_creds_file,
                    'token_file': 'tokens/work2.json',
                },
            ]
        }

        with pytest.raises(ConfigValidationError, match="Duplicate"):
            validate_config(cfg)

    def test_missing_credentials_file(self):
        """Should raise when credentials file doesn't exist."""
        cfg = {
            'calendars': [
                {
                    'name': 'WORK',
                    'calendar_id': 'primary',
                    'credentials_file': '/nonexistent/path.json',
                    'token_file': 'tokens/work.json',
                },
                {
                    'name': 'PERS',
                    'calendar_id': 'primary',
                    'credentials_file': '/nonexistent/path2.json',
                    'token_file': 'tokens/pers.json',
                },
            ]
        }

        with pytest.raises(ConfigValidationError, match="not found"):
            validate_config(cfg)

    def test_invalid_visibility(self, valid_config):
        """Should raise when visibility is invalid."""
        valid_config['default_copy_visibility'] = 'invalid_value'

        with pytest.raises(ConfigValidationError, match="visibility"):
            validate_config(valid_config)

    def test_valid_visibilities(self, valid_config):
        """Should accept all valid visibility values."""
        for vis in ['default', 'private', 'public', 'confidential']:
            valid_config['default_copy_visibility'] = vis
            validate_config(valid_config)  # Should not raise

    def test_invalid_window_days_past(self, valid_config):
        """Should raise when window_days_past is invalid."""
        valid_config['window_days_past'] = 'not_a_number'

        with pytest.raises(ConfigValidationError, match="window_days_past"):
            validate_config(valid_config)

    def test_negative_window_days(self, valid_config):
        """Should raise when window days is negative."""
        valid_config['window_days_past'] = -5

        with pytest.raises(ConfigValidationError, match="window_days_past"):
            validate_config(valid_config)

    def test_empty_calendars_list(self):
        """Should raise when calendars list is empty."""
        cfg = {'calendars': []}

        with pytest.raises(ConfigValidationError, match="2 calendars"):
            validate_config(cfg)


class TestLoadConfig:
    """Tests for load_config() function."""

    def test_load_yaml_config(self, valid_config, temp_creds_file):
        """Should load YAML configuration file."""
        with tempfile.NamedTemporaryFile(
            mode='w', suffix='.yaml', delete=False
        ) as f:
            yaml.dump(valid_config, f)
            config_path = f.name

        try:
            cfg = load_config(config_path)
            assert cfg['window_days_past'] == 30
            assert len(cfg['calendars']) == 2
        finally:
            os.unlink(config_path)

    def test_load_json_config(self, valid_config, temp_creds_file):
        """Should load JSON configuration file."""
        with tempfile.NamedTemporaryFile(
            mode='w', suffix='.json', delete=False
        ) as f:
            json.dump(valid_config, f)
            config_path = f.name

        try:
            cfg = load_config(config_path)
            assert cfg['window_days_past'] == 30
        finally:
            os.unlink(config_path)

    def test_load_nonexistent_file(self):
        """Should raise FileNotFoundError for missing file."""
        with pytest.raises(FileNotFoundError):
            load_config('/nonexistent/config.yaml')

    def test_load_validates_config(self, temp_creds_file):
        """load_config should validate the configuration."""
        invalid_config = {'calendars': []}  # Invalid: no calendars

        with tempfile.NamedTemporaryFile(
            mode='w', suffix='.yaml', delete=False
        ) as f:
            yaml.dump(invalid_config, f)
            config_path = f.name

        try:
            with pytest.raises(ConfigValidationError):
                load_config(config_path)
        finally:
            os.unlink(config_path)

    def test_load_yml_extension(self, valid_config, temp_creds_file):
        """Should handle .yml extension."""
        with tempfile.NamedTemporaryFile(
            mode='w', suffix='.yml', delete=False
        ) as f:
            yaml.dump(valid_config, f)
            config_path = f.name

        try:
            cfg = load_config(config_path)
            assert 'calendars' in cfg
        finally:
            os.unlink(config_path)
