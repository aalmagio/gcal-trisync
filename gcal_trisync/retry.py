"""
Retry and rate limiting utilities for gcal_trisync.

Provides decorators and utilities for handling API failures
with exponential backoff and rate limiting.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from functools import wraps
from threading import Lock
from typing import Any, Callable, Optional, TypeVar

from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    wait_random_exponential,
    before_sleep_log,
    after_log,
)

logger = logging.getLogger(__name__)

# Type variable for generic function return type
T = TypeVar('T')

# Google Calendar API error codes that should trigger retry
RETRYABLE_STATUS_CODES = frozenset({
    408,  # Request Timeout
    429,  # Too Many Requests (rate limit)
    500,  # Internal Server Error
    502,  # Bad Gateway
    503,  # Service Unavailable
    504,  # Gateway Timeout
})

# Error codes that indicate we should back off significantly
RATE_LIMIT_STATUS_CODES = frozenset({429})

# Default retry configuration
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_MIN_WAIT = 1  # seconds
DEFAULT_MAX_WAIT = 60  # seconds


class RetryableError(Exception):
    """
    Exception wrapper for retryable API errors.

    Attributes:
        status_code: HTTP status code from the API
        message: Error message
        original_error: The original exception
    """

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        original_error: Optional[Exception] = None
    ):
        super().__init__(message)
        self.status_code = status_code
        self.original_error = original_error
        self.message = message

    def __str__(self) -> str:
        if self.status_code:
            return f"[{self.status_code}] {self.message}"
        return self.message


class RateLimitError(RetryableError):
    """Specific error for rate limit (429) responses."""

    def __init__(
        self,
        message: str = "Rate limit exceeded",
        retry_after: Optional[int] = None,
        original_error: Optional[Exception] = None
    ):
        super().__init__(message, 429, original_error)
        self.retry_after = retry_after


def is_retryable_error(error: Exception) -> bool:
    """
    Check if an error should trigger a retry.

    Args:
        error: The exception to check

    Returns:
        True if the error is retryable
    """
    # Check our custom errors
    if isinstance(error, RetryableError):
        return True

    # Check Google API HttpError
    if hasattr(error, 'resp') and hasattr(error.resp, 'status'):
        return error.resp.status in RETRYABLE_STATUS_CODES

    # Check for common transient errors
    error_str = str(error).lower()
    transient_patterns = [
        'timeout',
        'connection reset',
        'connection refused',
        'temporary failure',
        'service unavailable',
    ]
    return any(pattern in error_str for pattern in transient_patterns)


def extract_retry_after(error: Exception) -> Optional[int]:
    """
    Extract Retry-After header value from an error.

    Args:
        error: The exception to check

    Returns:
        Retry-After seconds if present, None otherwise
    """
    if isinstance(error, RateLimitError):
        return error.retry_after

    if hasattr(error, 'resp') and hasattr(error.resp, 'headers'):
        retry_after = error.resp.headers.get('Retry-After')
        if retry_after:
            try:
                return int(retry_after)
            except ValueError:
                pass

    return None


def create_retry_decorator(
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    min_wait: float = DEFAULT_MIN_WAIT,
    max_wait: float = DEFAULT_MAX_WAIT,
    log_level: int = logging.WARNING
) -> Callable:
    """
    Create a retry decorator with exponential backoff.

    Args:
        max_attempts: Maximum number of retry attempts
        min_wait: Minimum wait time between retries (seconds)
        max_wait: Maximum wait time between retries (seconds)
        log_level: Log level for retry messages

    Returns:
        A decorator function
    """
    return retry(
        retry=retry_if_exception_type((RetryableError, Exception)),
        stop=stop_after_attempt(max_attempts),
        wait=wait_random_exponential(min=min_wait, max=max_wait),
        before_sleep=before_sleep_log(logger, log_level),
        after=after_log(logger, log_level),
        reraise=True,
    )


# Pre-configured retry decorator for API calls
api_retry = create_retry_decorator()


def with_retry(
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    min_wait: float = DEFAULT_MIN_WAIT,
    max_wait: float = DEFAULT_MAX_WAIT,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """
    Decorator for retrying functions with exponential backoff.

    Handles Google API errors and other transient failures.

    Args:
        max_attempts: Maximum number of retry attempts
        min_wait: Minimum wait time between retries (seconds)
        max_wait: Maximum wait time between retries (seconds)

    Returns:
        Decorated function

    Example:
        @with_retry(max_attempts=3)
        def call_api():
            return service.events().list(...).execute()
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            last_error: Optional[Exception] = None
            attempt = 0

            while attempt < max_attempts:
                attempt += 1
                try:
                    return func(*args, **kwargs)

                except Exception as e:
                    last_error = e

                    if not is_retryable_error(e):
                        raise

                    if attempt >= max_attempts:
                        logger.error(
                            f"Max retry attempts ({max_attempts}) reached "
                            f"for {func.__name__}: {e}"
                        )
                        raise

                    # Calculate wait time
                    retry_after = extract_retry_after(e)
                    if retry_after:
                        wait_time = min(retry_after, max_wait)
                    else:
                        # Exponential backoff with jitter
                        import random
                        base_wait = min_wait * (2 ** (attempt - 1))
                        jitter = random.uniform(0, base_wait * 0.1)
                        wait_time = min(base_wait + jitter, max_wait)

                    logger.warning(
                        f"Retry {attempt}/{max_attempts} for {func.__name__} "
                        f"after {wait_time:.1f}s: {e}"
                    )
                    time.sleep(wait_time)

            # Should not reach here, but just in case
            if last_error:
                raise last_error
            raise RuntimeError("Unexpected retry loop exit")

        return wrapper
    return decorator


@dataclass
class RateLimiter:
    """
    Token bucket rate limiter for API calls.

    Implements a token bucket algorithm to limit the rate of API calls.
    Thread-safe for concurrent use.

    Attributes:
        calls_per_second: Maximum calls per second
        burst_size: Maximum burst size (tokens)
    """
    calls_per_second: float = 10.0
    burst_size: int = 20
    _tokens: float = field(init=False)
    _last_update: float = field(init=False)
    _lock: Lock = field(init=False, default_factory=Lock)

    def __post_init__(self) -> None:
        self._tokens = float(self.burst_size)
        self._last_update = time.monotonic()

    def _refill(self) -> None:
        """Refill tokens based on elapsed time."""
        now = time.monotonic()
        elapsed = now - self._last_update
        self._tokens = min(
            self.burst_size,
            self._tokens + elapsed * self.calls_per_second
        )
        self._last_update = now

    def acquire(self, timeout: Optional[float] = None) -> bool:
        """
        Acquire a token, blocking if necessary.

        Args:
            timeout: Maximum time to wait (None for no limit)

        Returns:
            True if token acquired, False if timeout
        """
        start_time = time.monotonic()

        with self._lock:
            while True:
                self._refill()

                if self._tokens >= 1:
                    self._tokens -= 1
                    return True

                if timeout is not None:
                    elapsed = time.monotonic() - start_time
                    if elapsed >= timeout:
                        return False

                # Calculate wait time for next token
                wait_time = (1 - self._tokens) / self.calls_per_second

                if timeout is not None:
                    remaining = timeout - (time.monotonic() - start_time)
                    wait_time = min(wait_time, remaining)

                if wait_time > 0:
                    # Release lock while sleeping
                    self._lock.release()
                    try:
                        time.sleep(wait_time)
                    finally:
                        self._lock.acquire()

    def __enter__(self) -> 'RateLimiter':
        self.acquire()
        return self

    def __exit__(self, *args: Any) -> None:
        pass


# Default rate limiter for Google Calendar API
# Google Calendar API has a limit of ~10 requests/second
default_rate_limiter = RateLimiter(calls_per_second=8.0, burst_size=15)


def rate_limited(
    limiter: Optional[RateLimiter] = None
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """
    Decorator to rate limit function calls.

    Args:
        limiter: RateLimiter instance (uses default if None)

    Returns:
        Decorated function

    Example:
        @rate_limited()
        def call_api():
            return service.events().list(...).execute()
    """
    if limiter is None:
        limiter = default_rate_limiter

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            limiter.acquire()
            return func(*args, **kwargs)
        return wrapper
    return decorator


def robust_api_call(
    func: Callable[..., T],
    *args: Any,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    rate_limiter: Optional[RateLimiter] = None,
    **kwargs: Any
) -> T:
    """
    Execute an API call with retry and rate limiting.

    Convenience function that combines retry logic and rate limiting.

    Args:
        func: Function to call
        *args: Positional arguments for func
        max_attempts: Maximum retry attempts
        rate_limiter: RateLimiter to use (default if None)
        **kwargs: Keyword arguments for func

    Returns:
        Result of func

    Example:
        result = robust_api_call(
            service.events().list,
            calendarId='primary',
            maxResults=100
        ).execute()
    """
    if rate_limiter is None:
        rate_limiter = default_rate_limiter

    @with_retry(max_attempts=max_attempts)
    def _call() -> T:
        rate_limiter.acquire()
        return func(*args, **kwargs)

    return _call()
