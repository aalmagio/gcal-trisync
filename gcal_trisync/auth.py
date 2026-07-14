"""
Authentication and token management for gcal_trisync.

Provides robust OAuth2 token handling with:
- Proactive token refresh before expiry
- Token health status checking
- Graceful handling of revoked/invalid tokens
- Per-account token management
- Detailed auth event logging
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

SCOPES = ['https://www.googleapis.com/auth/calendar']

# Refresh tokens when less than this many seconds remain
DEFAULT_REFRESH_MARGIN_S = 300  # 5 minutes


class TokenStatus(Enum):
    """Status of an OAuth token."""
    VALID = 'valid'
    EXPIRING_SOON = 'expiring_soon'
    EXPIRED = 'expired'
    MISSING = 'missing'
    INVALID = 'invalid'
    REVOKED = 'revoked'


class AuthError(Exception):
    """Authentication error with descriptive context."""

    def __init__(
        self,
        message: str,
        account: Optional[str] = None,
        status: Optional[TokenStatus] = None,
        original_error: Optional[Exception] = None
    ):
        self.account = account
        self.status = status
        self.original_error = original_error
        prefix = f"[{account}] " if account else ""
        super().__init__(f"{prefix}{message}")


@dataclass
class TokenInfo:
    """
    Information about an OAuth token's current state.

    Attributes:
        status: Current token status
        token_file: Path to the token file
        account: Account name/label
        expiry: Token expiry time (if known)
        scopes: Granted scopes (if known)
        message: Human-readable status description
    """
    status: TokenStatus
    token_file: str
    account: str = ''
    expiry: Optional[datetime] = None
    scopes: Optional[list[str]] = None
    message: str = ''

    def is_usable(self) -> bool:
        """Check if the token can be used for API calls."""
        return self.status in (TokenStatus.VALID, TokenStatus.EXPIRING_SOON)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            'status': self.status.value,
            'token_file': self.token_file,
            'account': self.account,
            'expiry': self.expiry.isoformat() if self.expiry else None,
            'scopes': self.scopes,
            'message': self.message,
        }


class TokenManager:
    """
    Manages OAuth2 tokens for Google Calendar API.

    Handles loading, refreshing, validating, and saving tokens
    with robust error handling and proactive refresh.

    Args:
        credentials_file: Path to OAuth client credentials JSON
        token_file: Path to store/retrieve the access token
        account_name: Label for this account (for logging)
        refresh_margin_s: Seconds before expiry to trigger proactive refresh
    """

    def __init__(
        self,
        credentials_file: str,
        token_file: str,
        account_name: str = '',
        refresh_margin_s: int = DEFAULT_REFRESH_MARGIN_S
    ) -> None:
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.account_name = account_name or token_file
        self.refresh_margin_s = refresh_margin_s
        self._creds: Optional[Credentials] = None

    @property
    def credentials(self) -> Optional[Credentials]:
        """Access the current credentials object."""
        return self._creds

    def check_status(self) -> TokenInfo:
        """
        Check the current status of the token without modifying it.

        Returns:
            TokenInfo with current token state
        """
        if not os.path.exists(self.token_file):
            return TokenInfo(
                status=TokenStatus.MISSING,
                token_file=self.token_file,
                account=self.account_name,
                message='Token file not found — authentication required'
            )

        try:
            creds = Credentials.from_authorized_user_file(
                self.token_file, SCOPES
            )
        except (json.JSONDecodeError, ValueError, KeyError) as e:
            return TokenInfo(
                status=TokenStatus.INVALID,
                token_file=self.token_file,
                account=self.account_name,
                message=f'Token file is corrupt or unreadable: {e}'
            )

        expiry = creds.expiry
        if expiry and expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)

        scopes = list(creds.scopes) if creds.scopes else None

        if not creds.valid:
            if creds.expired:
                if creds.refresh_token:
                    return TokenInfo(
                        status=TokenStatus.EXPIRED,
                        token_file=self.token_file,
                        account=self.account_name,
                        expiry=expiry,
                        scopes=scopes,
                        message='Token expired — will be refreshed automatically'
                    )
                else:
                    return TokenInfo(
                        status=TokenStatus.REVOKED,
                        token_file=self.token_file,
                        account=self.account_name,
                        expiry=expiry,
                        scopes=scopes,
                        message='Token expired and no refresh token — re-authentication required'
                    )
            return TokenInfo(
                status=TokenStatus.INVALID,
                token_file=self.token_file,
                account=self.account_name,
                expiry=expiry,
                scopes=scopes,
                message='Token is not valid'
            )

        # Check if expiring soon
        if expiry:
            now = datetime.now(timezone.utc)
            remaining = (expiry - now).total_seconds()
            if remaining < self.refresh_margin_s:
                return TokenInfo(
                    status=TokenStatus.EXPIRING_SOON,
                    token_file=self.token_file,
                    account=self.account_name,
                    expiry=expiry,
                    scopes=scopes,
                    message=f'Token expires in {int(remaining)}s — will refresh proactively'
                )

        return TokenInfo(
            status=TokenStatus.VALID,
            token_file=self.token_file,
            account=self.account_name,
            expiry=expiry,
            scopes=scopes,
            message='Token is valid'
        )

    def load(self) -> Optional[Credentials]:
        """
        Load credentials from the token file.

        Returns:
            Credentials if loaded successfully, None otherwise
        """
        if not os.path.exists(self.token_file):
            logger.debug(f"[{self.account_name}] No token file found")
            return None

        try:
            self._creds = Credentials.from_authorized_user_file(
                self.token_file, SCOPES
            )
            logger.debug(f"[{self.account_name}] Token loaded from {self.token_file}")
            return self._creds
        except (json.JSONDecodeError, ValueError, KeyError) as e:
            logger.warning(
                f"[{self.account_name}] Token file corrupt, removing: {e}"
            )
            os.remove(self.token_file)
            return None

    def save(self) -> None:
        """Save current credentials to the token file."""
        if not self._creds:
            return

        # Ensure directory exists
        token_dir = os.path.dirname(self.token_file)
        if token_dir:
            os.makedirs(token_dir, exist_ok=True)

        # Restrict permissions: the token grants full calendar access
        fd = os.open(
            self.token_file,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600
        )
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(self._creds.to_json())

        logger.debug(f"[{self.account_name}] Token saved to {self.token_file}")

    def refresh(self) -> bool:
        """
        Refresh the access token using the refresh token.

        Returns:
            True if refresh succeeded, False otherwise

        Raises:
            AuthError: If token is revoked or refresh fails permanently
        """
        if not self._creds:
            return False

        if not self._creds.refresh_token:
            raise AuthError(
                "No refresh token available — re-authentication required",
                account=self.account_name,
                status=TokenStatus.REVOKED
            )

        try:
            self._creds.refresh(Request())
            self.save()
            logger.info(f"[{self.account_name}] Token refreshed successfully")
            return True
        except RefreshError as e:
            error_msg = str(e).lower()
            if 'revoked' in error_msg or 'invalid_grant' in error_msg:
                logger.error(
                    f"[{self.account_name}] Token has been revoked — "
                    "re-authentication required"
                )
                raise AuthError(
                    "Token has been revoked — re-authentication required. "
                    "Run with --reauth to re-authenticate.",
                    account=self.account_name,
                    status=TokenStatus.REVOKED,
                    original_error=e
                ) from e
            else:
                logger.error(
                    f"[{self.account_name}] Token refresh failed: {e}"
                )
                raise AuthError(
                    f"Token refresh failed: {e}",
                    account=self.account_name,
                    status=TokenStatus.INVALID,
                    original_error=e
                ) from e

    def ensure_valid(self) -> Credentials:
        """
        Ensure credentials are valid, refreshing proactively if needed.

        This is the main method for getting usable credentials.
        It loads, validates, and refreshes tokens as needed.

        Returns:
            Valid Credentials object

        Raises:
            AuthError: If credentials cannot be obtained
        """
        if not self._creds:
            self.load()

        if not self._creds:
            raise AuthError(
                "No credentials available — authentication required",
                account=self.account_name,
                status=TokenStatus.MISSING
            )

        if self._creds.valid:
            # Check if expiring soon and proactively refresh
            if self._creds.expiry:
                expiry = self._creds.expiry
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
                remaining = (expiry - datetime.now(timezone.utc)).total_seconds()
                if remaining < self.refresh_margin_s:
                    logger.info(
                        f"[{self.account_name}] Token expires in {int(remaining)}s, "
                        "refreshing proactively"
                    )
                    self.refresh()
            return self._creds

        # Token is not valid — try to refresh
        if self._creds.expired and self._creds.refresh_token:
            self.refresh()
            return self._creds

        raise AuthError(
            "Token is invalid and cannot be refreshed — "
            "re-authentication required",
            account=self.account_name,
            status=TokenStatus.INVALID
        )

    def authenticate(
        self,
        auth_method: str = 'local',
        login_hint: Optional[str] = None,
        port: int = 0
    ) -> Credentials:
        """
        Run the full OAuth2 authentication flow.

        Args:
            auth_method: 'local' for browser flow, 'console' for manual code entry
            login_hint: Email to pre-fill in login
            port: Port for local OAuth server (0 for auto-select)

        Returns:
            New Credentials object

        Raises:
            AuthError: If authentication fails
        """
        if not os.path.exists(self.credentials_file):
            raise AuthError(
                f"Credentials file not found: {self.credentials_file}",
                account=self.account_name,
                status=TokenStatus.MISSING
            )

        try:
            flow = InstalledAppFlow.from_client_secrets_file(
                self.credentials_file, SCOPES, redirect_uri=None
            )

            if auth_method == 'console':
                auth_url, _ = flow.authorization_url(
                    access_type='offline',
                    include_granted_scopes='true',
                    prompt='consent',
                    login_hint=login_hint
                )
                print(
                    "\nOpen this URL in an incognito window "
                    "and paste the code here:\n"
                )
                print(auth_url)
                code = input("\nCode: ").strip()
                flow.fetch_token(code=code)
                self._creds = flow.credentials
            else:
                self._creds = flow.run_local_server(
                    port=port,
                    prompt='consent',
                    authorization_prompt_message=None,
                    login_hint=login_hint
                )

            self.save()
            logger.info(f"[{self.account_name}] Authentication successful")
            return self._creds

        except Exception as e:
            raise AuthError(
                f"Authentication failed: {e}",
                account=self.account_name,
                original_error=e
            ) from e

    def get_service(
        self,
        auth_method: str = 'local',
        login_hint: Optional[str] = None,
        port: int = 0,
        force_reauth: bool = False
    ) -> Any:
        """
        Get an authenticated Google Calendar API service.

        Handles the full lifecycle: load → validate → refresh → auth → build.

        Args:
            auth_method: 'local' for browser flow, 'console' for manual code entry
            login_hint: Email to pre-fill in login
            port: Port for local OAuth server (0 for auto-select)
            force_reauth: Force re-authentication even if token is valid

        Returns:
            Google Calendar API service object

        Raises:
            AuthError: If authentication cannot be completed
        """
        if force_reauth:
            logger.info(f"[{self.account_name}] Forcing re-authentication")
            self.authenticate(auth_method, login_hint, port)
        else:
            # Try to load and validate existing token
            self.load()

            if self._creds:
                try:
                    self.ensure_valid()
                except AuthError as e:
                    if e.status == TokenStatus.REVOKED:
                        raise
                    # Other auth errors — try full auth flow
                    logger.warning(
                        f"[{self.account_name}] Token unusable, "
                        "starting authentication flow"
                    )
                    self.authenticate(auth_method, login_hint, port)
            else:
                # No existing token — run auth flow
                self.authenticate(auth_method, login_hint, port)

        return build(
            'calendar', 'v3',
            credentials=self._creds,
            cache_discovery=False
        )


def check_all_tokens(
    calendar_configs: list[dict[str, Any]],
    refresh_margin_s: int = DEFAULT_REFRESH_MARGIN_S
) -> list[TokenInfo]:
    """
    Check token status for all configured calendars.

    Args:
        calendar_configs: List of calendar configuration dictionaries
        refresh_margin_s: Seconds before expiry to flag as expiring

    Returns:
        List of TokenInfo for each calendar
    """
    results: list[TokenInfo] = []

    for cfg in calendar_configs:
        mgr = TokenManager(
            credentials_file=cfg['credentials_file'],
            token_file=cfg['token_file'],
            account_name=cfg['name'],
            refresh_margin_s=refresh_margin_s
        )
        info = mgr.check_status()
        results.append(info)

    return results


def format_token_report(infos: list[TokenInfo]) -> str:
    """
    Format token status information as a human-readable report.

    Args:
        infos: List of TokenInfo to display

    Returns:
        Formatted report string
    """
    lines: list[str] = []
    lines.append('=' * 55)
    lines.append('  TRISYNC - Authentication Status')
    lines.append('=' * 55)
    lines.append('')

    status_symbols = {
        TokenStatus.VALID: 'OK',
        TokenStatus.EXPIRING_SOON: 'WARN',
        TokenStatus.EXPIRED: 'REFRESH',
        TokenStatus.MISSING: 'MISSING',
        TokenStatus.INVALID: 'ERROR',
        TokenStatus.REVOKED: 'REVOKED',
    }

    all_ok = True
    for info in infos:
        symbol = status_symbols.get(info.status, '?')
        lines.append(f'  [{symbol:^8}] {info.account}')
        lines.append(f'             {info.message}')
        if info.expiry:
            lines.append(f'             Expires: {info.expiry.isoformat()}')
        lines.append(f'             File: {info.token_file}')
        lines.append('')
        if not info.is_usable() and info.status != TokenStatus.EXPIRED:
            all_ok = False

    lines.append('-' * 55)
    if all_ok:
        lines.append('  All accounts are ready for sync.')
    else:
        lines.append(
            '  Some accounts need attention. '
            'Use --reauth to re-authenticate.'
        )
    lines.append('=' * 55)

    return '\n'.join(lines)
