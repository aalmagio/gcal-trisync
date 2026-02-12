"""Tests for authentication and token management in gcal_trisync."""

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

# We need to mock Google auth modules before importing auth.py
# Create mock modules
_mock_google_auth = MagicMock()
_mock_google_auth_transport = MagicMock()
_mock_google_auth_transport_requests = MagicMock()
_mock_google_oauth2 = MagicMock()
_mock_google_oauth2_credentials = MagicMock()
_mock_google_auth_oauthlib = MagicMock()
_mock_google_auth_oauthlib_flow = MagicMock()
_mock_googleapiclient = MagicMock()
_mock_googleapiclient_discovery = MagicMock()

# RefreshError for testing
class MockRefreshError(Exception):
    pass

_mock_google_auth.exceptions = MagicMock()
_mock_google_auth.exceptions.RefreshError = MockRefreshError

# Install mocks
sys.modules['google'] = MagicMock()
sys.modules['google.auth'] = _mock_google_auth
sys.modules['google.auth.exceptions'] = _mock_google_auth.exceptions
sys.modules['google.auth.transport'] = _mock_google_auth_transport
sys.modules['google.auth.transport.requests'] = _mock_google_auth_transport_requests
sys.modules['google.oauth2'] = _mock_google_oauth2
sys.modules['google.oauth2.credentials'] = _mock_google_oauth2_credentials
sys.modules['google_auth_oauthlib'] = _mock_google_auth_oauthlib
sys.modules['google_auth_oauthlib.flow'] = _mock_google_auth_oauthlib_flow
sys.modules['googleapiclient'] = _mock_googleapiclient
sys.modules['googleapiclient.discovery'] = _mock_googleapiclient_discovery
sys.modules['googleapiclient.errors'] = MagicMock()

# Now import auth module
from importlib.util import spec_from_file_location, module_from_spec

_auth_path = Path(__file__).parent.parent / 'gcal_trisync' / 'auth.py'
_spec = spec_from_file_location('gcal_trisync.auth', _auth_path)
_auth = module_from_spec(_spec)
sys.modules['gcal_trisync.auth'] = _auth

# Patch the Google imports inside auth module before loading
_auth.RefreshError = MockRefreshError
_spec.loader.exec_module(_auth)

# Override RefreshError after module load
_auth.RefreshError = MockRefreshError

TokenStatus = _auth.TokenStatus
TokenInfo = _auth.TokenInfo
TokenManager = _auth.TokenManager
AuthError = _auth.AuthError
check_all_tokens = _auth.check_all_tokens
format_token_report = _auth.format_token_report
DEFAULT_REFRESH_MARGIN_S = _auth.DEFAULT_REFRESH_MARGIN_S


# ── TokenStatus Tests ───────────────────────────────────────────────

class TestTokenStatus:
    """Tests for TokenStatus enum."""

    def test_all_statuses_exist(self):
        """Should have all expected statuses."""
        assert TokenStatus.VALID.value == 'valid'
        assert TokenStatus.EXPIRING_SOON.value == 'expiring_soon'
        assert TokenStatus.EXPIRED.value == 'expired'
        assert TokenStatus.MISSING.value == 'missing'
        assert TokenStatus.INVALID.value == 'invalid'
        assert TokenStatus.REVOKED.value == 'revoked'

    def test_status_count(self):
        """Should have exactly 6 statuses."""
        assert len(TokenStatus) == 6


# ── TokenInfo Tests ─────────────────────────────────────────────────

class TestTokenInfo:
    """Tests for TokenInfo dataclass."""

    def test_create_valid(self):
        """Should create with valid status."""
        info = TokenInfo(
            status=TokenStatus.VALID,
            token_file='tokens/test.json',
            account='TEST',
            message='Token is valid'
        )
        assert info.status == TokenStatus.VALID
        assert info.account == 'TEST'
        assert info.is_usable() is True

    def test_create_missing(self):
        """Should mark missing as not usable."""
        info = TokenInfo(
            status=TokenStatus.MISSING,
            token_file='tokens/test.json'
        )
        assert info.is_usable() is False

    def test_expiring_soon_is_usable(self):
        """Expiring soon should still be usable."""
        info = TokenInfo(
            status=TokenStatus.EXPIRING_SOON,
            token_file='tokens/test.json'
        )
        assert info.is_usable() is True

    def test_expired_is_not_usable(self):
        """Expired should not be usable."""
        info = TokenInfo(
            status=TokenStatus.EXPIRED,
            token_file='tokens/test.json'
        )
        assert info.is_usable() is False

    def test_revoked_is_not_usable(self):
        """Revoked should not be usable."""
        info = TokenInfo(
            status=TokenStatus.REVOKED,
            token_file='tokens/test.json'
        )
        assert info.is_usable() is False

    def test_to_dict(self):
        """Should serialize to dictionary."""
        expiry = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        info = TokenInfo(
            status=TokenStatus.VALID,
            token_file='tokens/test.json',
            account='WORK',
            expiry=expiry,
            scopes=['calendar'],
            message='OK'
        )
        d = info.to_dict()
        assert d['status'] == 'valid'
        assert d['account'] == 'WORK'
        assert d['expiry'] is not None
        assert d['scopes'] == ['calendar']
        assert d['message'] == 'OK'

    def test_to_dict_no_expiry(self):
        """Should handle None expiry."""
        info = TokenInfo(
            status=TokenStatus.MISSING,
            token_file='tokens/test.json'
        )
        d = info.to_dict()
        assert d['expiry'] is None


# ── AuthError Tests ─────────────────────────────────────────────────

class TestAuthError:
    """Tests for AuthError exception."""

    def test_basic_error(self):
        """Should create basic auth error."""
        err = AuthError("failed")
        assert str(err) == "failed"
        assert err.account is None
        assert err.status is None

    def test_error_with_account(self):
        """Should include account in message."""
        err = AuthError("failed", account="WORK")
        assert "[WORK]" in str(err)
        assert "failed" in str(err)

    def test_error_with_status(self):
        """Should store status."""
        err = AuthError("revoked", status=TokenStatus.REVOKED)
        assert err.status == TokenStatus.REVOKED

    def test_error_with_original(self):
        """Should preserve original exception."""
        original = ValueError("orig")
        err = AuthError("wrapped", original_error=original)
        assert err.original_error is original

    def test_is_exception(self):
        """Should be catchable as Exception."""
        with pytest.raises(Exception):
            raise AuthError("test")


# ── TokenManager Tests ──────────────────────────────────────────────

class TestTokenManager:
    """Tests for TokenManager class."""

    def test_create(self):
        """Should create with paths and name."""
        mgr = TokenManager(
            credentials_file='creds/work.json',
            token_file='tokens/work.json',
            account_name='WORK'
        )
        assert mgr.credentials_file == 'creds/work.json'
        assert mgr.token_file == 'tokens/work.json'
        assert mgr.account_name == 'WORK'
        assert mgr.credentials is None

    def test_default_account_name(self):
        """Should default account_name to token_file."""
        mgr = TokenManager(
            credentials_file='creds/x.json',
            token_file='tokens/x.json'
        )
        assert mgr.account_name == 'tokens/x.json'

    def test_default_refresh_margin(self):
        """Should use default refresh margin."""
        mgr = TokenManager(
            credentials_file='x', token_file='y'
        )
        assert mgr.refresh_margin_s == DEFAULT_REFRESH_MARGIN_S

    def test_custom_refresh_margin(self):
        """Should accept custom refresh margin."""
        mgr = TokenManager(
            credentials_file='x', token_file='y',
            refresh_margin_s=600
        )
        assert mgr.refresh_margin_s == 600


class TestTokenManagerCheckStatus:
    """Tests for TokenManager.check_status()."""

    def test_missing_token_file(self):
        """Should return MISSING when token file doesn't exist."""
        mgr = TokenManager(
            credentials_file='creds/x.json',
            token_file='/tmp/nonexistent_token_test.json',
            account_name='TEST'
        )
        info = mgr.check_status()
        assert info.status == TokenStatus.MISSING
        assert 'not found' in info.message

    def test_corrupt_token_file(self):
        """Should return INVALID for corrupt token files."""
        with tempfile.NamedTemporaryFile(
            mode='w', suffix='.json', delete=False
        ) as f:
            f.write('not valid json{{{')
            filepath = f.name

        try:
            mgr = TokenManager(
                credentials_file='creds/x.json',
                token_file=filepath,
                account_name='TEST'
            )
            # Mock the Credentials.from_authorized_user_file to raise
            with patch.object(
                _auth, 'Credentials'
            ) as mock_creds_cls:
                mock_creds_cls.from_authorized_user_file.side_effect = \
                    json.JSONDecodeError("bad", "", 0)
                info = mgr.check_status()
            assert info.status == TokenStatus.INVALID
            assert 'corrupt' in info.message
        finally:
            os.unlink(filepath)


class TestTokenManagerLoad:
    """Tests for TokenManager.load()."""

    def test_load_missing_file(self):
        """Should return None for missing file."""
        mgr = TokenManager(
            credentials_file='x',
            token_file='/tmp/nonexistent_test_token.json'
        )
        assert mgr.load() is None

    def test_load_corrupt_file_removes_it(self):
        """Should remove corrupt token file and return None."""
        with tempfile.NamedTemporaryFile(
            mode='w', suffix='.json', delete=False
        ) as f:
            f.write('corrupt data')
            filepath = f.name

        try:
            mgr = TokenManager(
                credentials_file='x',
                token_file=filepath
            )
            with patch.object(
                _auth, 'Credentials'
            ) as mock_creds_cls:
                mock_creds_cls.from_authorized_user_file.side_effect = \
                    json.JSONDecodeError("bad", "", 0)
                result = mgr.load()
            assert result is None
            assert not os.path.exists(filepath)
        finally:
            if os.path.exists(filepath):
                os.unlink(filepath)


class TestTokenManagerSave:
    """Tests for TokenManager.save()."""

    def test_save_no_creds(self):
        """Should do nothing if no credentials."""
        mgr = TokenManager(
            credentials_file='x',
            token_file='/tmp/test_save_token.json'
        )
        mgr.save()  # Should not raise

    def test_save_creates_directory(self):
        """Should create parent directory if needed."""
        tmpdir = tempfile.mkdtemp()
        token_path = os.path.join(tmpdir, 'subdir', 'token.json')

        mgr = TokenManager(
            credentials_file='x',
            token_file=token_path
        )
        mock_creds = MagicMock()
        mock_creds.to_json.return_value = '{"token": "test"}'
        mgr._creds = mock_creds

        mgr.save()
        assert os.path.exists(token_path)

        # Cleanup
        os.unlink(token_path)
        os.rmdir(os.path.dirname(token_path))
        os.rmdir(tmpdir)


class TestTokenManagerRefresh:
    """Tests for TokenManager.refresh()."""

    def test_refresh_no_creds(self):
        """Should return False without credentials."""
        mgr = TokenManager(credentials_file='x', token_file='y')
        assert mgr.refresh() is False

    def test_refresh_no_refresh_token(self):
        """Should raise AuthError without refresh token."""
        mgr = TokenManager(
            credentials_file='x', token_file='y',
            account_name='TEST'
        )
        mock_creds = MagicMock()
        mock_creds.refresh_token = None
        mgr._creds = mock_creds

        with pytest.raises(AuthError) as exc_info:
            mgr.refresh()
        assert exc_info.value.status == TokenStatus.REVOKED

    def test_refresh_success(self):
        """Should refresh and save on success."""
        tmpfile = tempfile.NamedTemporaryFile(
            mode='w', suffix='.json', delete=False
        )
        tmpfile.close()

        try:
            mgr = TokenManager(
                credentials_file='x',
                token_file=tmpfile.name,
                account_name='TEST'
            )
            mock_creds = MagicMock()
            mock_creds.refresh_token = 'refresh_tok'
            mock_creds.to_json.return_value = '{"saved": true}'
            mgr._creds = mock_creds

            result = mgr.refresh()
            assert result is True
            mock_creds.refresh.assert_called_once()
        finally:
            if os.path.exists(tmpfile.name):
                os.unlink(tmpfile.name)

    def test_refresh_revoked_token(self):
        """Should raise AuthError with REVOKED status for revoked tokens."""
        mgr = TokenManager(
            credentials_file='x', token_file='y',
            account_name='TEST'
        )
        mock_creds = MagicMock()
        mock_creds.refresh_token = 'refresh_tok'
        mock_creds.refresh.side_effect = MockRefreshError(
            "Token has been revoked"
        )
        mgr._creds = mock_creds

        with pytest.raises(AuthError) as exc_info:
            mgr.refresh()
        assert exc_info.value.status == TokenStatus.REVOKED

    def test_refresh_invalid_grant(self):
        """Should treat invalid_grant as revoked."""
        mgr = TokenManager(
            credentials_file='x', token_file='y',
            account_name='TEST'
        )
        mock_creds = MagicMock()
        mock_creds.refresh_token = 'refresh_tok'
        mock_creds.refresh.side_effect = MockRefreshError("invalid_grant")
        mgr._creds = mock_creds

        with pytest.raises(AuthError) as exc_info:
            mgr.refresh()
        assert exc_info.value.status == TokenStatus.REVOKED

    def test_refresh_other_error(self):
        """Should raise AuthError with INVALID for other errors."""
        mgr = TokenManager(
            credentials_file='x', token_file='y',
            account_name='TEST'
        )
        mock_creds = MagicMock()
        mock_creds.refresh_token = 'refresh_tok'
        mock_creds.refresh.side_effect = MockRefreshError("network error")
        mgr._creds = mock_creds

        with pytest.raises(AuthError) as exc_info:
            mgr.refresh()
        assert exc_info.value.status == TokenStatus.INVALID


class TestTokenManagerEnsureValid:
    """Tests for TokenManager.ensure_valid()."""

    def test_no_creds_no_file(self):
        """Should raise AuthError when no credentials available."""
        mgr = TokenManager(
            credentials_file='x',
            token_file='/tmp/nonexistent_ensure_valid.json',
            account_name='TEST'
        )
        with pytest.raises(AuthError) as exc_info:
            mgr.ensure_valid()
        assert exc_info.value.status == TokenStatus.MISSING

    def test_valid_creds_returned(self):
        """Should return valid credentials directly."""
        mgr = TokenManager(
            credentials_file='x', token_file='y'
        )
        mock_creds = MagicMock()
        mock_creds.valid = True
        mock_creds.expiry = None
        mgr._creds = mock_creds

        result = mgr.ensure_valid()
        assert result is mock_creds


class TestTokenManagerAuthenticate:
    """Tests for TokenManager.authenticate()."""

    def test_missing_credentials_file(self):
        """Should raise AuthError for missing credentials file."""
        mgr = TokenManager(
            credentials_file='/tmp/nonexistent_creds_test.json',
            token_file='y',
            account_name='TEST'
        )
        with pytest.raises(AuthError) as exc_info:
            mgr.authenticate()
        assert 'not found' in str(exc_info.value)


# ── check_all_tokens Tests ──────────────────────────────────────────

class TestCheckAllTokens:
    """Tests for check_all_tokens function."""

    def test_returns_info_for_each_calendar(self):
        """Should return TokenInfo for each calendar config."""
        configs = [
            {
                'name': 'WORK',
                'credentials_file': 'creds/work.json',
                'token_file': '/tmp/nonexistent_work.json',
            },
            {
                'name': 'PERS',
                'credentials_file': 'creds/pers.json',
                'token_file': '/tmp/nonexistent_pers.json',
            },
        ]
        results = check_all_tokens(configs)
        assert len(results) == 2
        assert results[0].account == 'WORK'
        assert results[1].account == 'PERS'
        # Both files don't exist
        assert results[0].status == TokenStatus.MISSING
        assert results[1].status == TokenStatus.MISSING


# ── format_token_report Tests ───────────────────────────────────────

class TestFormatTokenReport:
    """Tests for format_token_report function."""

    def test_report_header(self):
        """Should include header."""
        report = format_token_report([])
        assert 'Authentication Status' in report

    def test_report_valid_account(self):
        """Should show OK for valid account."""
        infos = [
            TokenInfo(
                status=TokenStatus.VALID,
                token_file='tokens/work.json',
                account='WORK',
                message='Token is valid'
            )
        ]
        report = format_token_report(infos)
        assert 'OK' in report
        assert 'WORK' in report
        assert 'ready for sync' in report

    def test_report_missing_account(self):
        """Should show MISSING for missing token."""
        infos = [
            TokenInfo(
                status=TokenStatus.MISSING,
                token_file='tokens/x.json',
                account='MISSING_ACCT',
                message='Token file not found'
            )
        ]
        report = format_token_report(infos)
        assert 'MISSING' in report
        assert 'need attention' in report

    def test_report_expiring_soon(self):
        """Should show WARN for expiring token."""
        infos = [
            TokenInfo(
                status=TokenStatus.EXPIRING_SOON,
                token_file='tokens/x.json',
                account='EXPIRING',
                message='Expires soon',
                expiry=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
            )
        ]
        report = format_token_report(infos)
        assert 'WARN' in report
        assert 'Expires:' in report

    def test_report_revoked(self):
        """Should show REVOKED and suggest --reauth."""
        infos = [
            TokenInfo(
                status=TokenStatus.REVOKED,
                token_file='tokens/x.json',
                account='REVOKED_ACCT',
                message='Token revoked'
            )
        ]
        report = format_token_report(infos)
        assert 'REVOKED' in report
        assert '--reauth' in report

    def test_report_mixed_status(self):
        """Should report correctly with mixed statuses."""
        infos = [
            TokenInfo(
                status=TokenStatus.VALID,
                token_file='a',
                account='OK_ACCT',
                message='OK'
            ),
            TokenInfo(
                status=TokenStatus.REVOKED,
                token_file='b',
                account='BAD_ACCT',
                message='Revoked'
            ),
        ]
        report = format_token_report(infos)
        assert 'need attention' in report

    def test_report_expired_is_ok(self):
        """Expired tokens (with refresh) should count as OK."""
        infos = [
            TokenInfo(
                status=TokenStatus.EXPIRED,
                token_file='a',
                account='EXP_ACCT',
                message='Will be refreshed'
            ),
        ]
        report = format_token_report(infos)
        # Expired tokens can be refreshed, so "all ready" should appear
        assert 'ready for sync' in report
