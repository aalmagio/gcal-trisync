"""Tests for retry and rate limiting utilities in gcal_trisync."""

import sys
import time
from importlib.util import spec_from_file_location, module_from_spec
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Import retry module directly to avoid Google API dependencies
_retry_path = Path(__file__).parent.parent / 'gcal_trisync' / 'retry.py'
_spec = spec_from_file_location('gcal_trisync.retry', _retry_path)
_retry = module_from_spec(_spec)
sys.modules['gcal_trisync.retry'] = _retry
_spec.loader.exec_module(_retry)

RetryableError = _retry.RetryableError
RateLimitError = _retry.RateLimitError
RateLimiter = _retry.RateLimiter
is_retryable_error = _retry.is_retryable_error
extract_retry_after = _retry.extract_retry_after
with_retry = _retry.with_retry
rate_limited = _retry.rate_limited
robust_api_call = _retry.robust_api_call
RETRYABLE_STATUS_CODES = _retry.RETRYABLE_STATUS_CODES
default_rate_limiter = _retry.default_rate_limiter


# ── RetryableError Tests ────────────────────────────────────────────

class TestRetryableError:
    """Tests for RetryableError exception."""

    def test_basic_creation(self):
        """Should create error with message."""
        err = RetryableError("something failed")
        assert str(err) == "something failed"
        assert err.status_code is None
        assert err.original_error is None

    def test_with_status_code(self):
        """Should format with status code."""
        err = RetryableError("server error", status_code=500)
        assert str(err) == "[500] server error"
        assert err.status_code == 500

    def test_with_original_error(self):
        """Should preserve original exception."""
        original = ValueError("original")
        err = RetryableError("wrapped", original_error=original)
        assert err.original_error is original

    def test_is_exception(self):
        """Should be catchable as Exception."""
        with pytest.raises(Exception):
            raise RetryableError("test")


# ── RateLimitError Tests ────────────────────────────────────────────

class TestRateLimitError:
    """Tests for RateLimitError exception."""

    def test_default_message(self):
        """Should have default message."""
        err = RateLimitError()
        assert "Rate limit exceeded" in str(err)
        assert err.status_code == 429

    def test_custom_message(self):
        """Should accept custom message."""
        err = RateLimitError("too fast")
        assert "too fast" in str(err)

    def test_retry_after(self):
        """Should store retry_after value."""
        err = RateLimitError(retry_after=30)
        assert err.retry_after == 30

    def test_is_retryable_error(self):
        """Should be a RetryableError subclass."""
        err = RateLimitError()
        assert isinstance(err, RetryableError)


# ── is_retryable_error Tests ────────────────────────────────────────

class TestIsRetryableError:
    """Tests for is_retryable_error function."""

    def test_retryable_error_instance(self):
        """Should return True for RetryableError."""
        assert is_retryable_error(RetryableError("test")) is True

    def test_rate_limit_error_instance(self):
        """Should return True for RateLimitError."""
        assert is_retryable_error(RateLimitError()) is True

    def test_http_error_retryable_status(self):
        """Should return True for retryable HTTP status codes."""
        for code in RETRYABLE_STATUS_CODES:
            err = MagicMock()
            err.resp = MagicMock()
            err.resp.status = code
            assert is_retryable_error(err) is True, f"Status {code} should be retryable"

    def test_http_error_non_retryable_status(self):
        """Should return False for non-retryable HTTP status codes."""
        for code in [400, 401, 403, 404, 409]:
            err = MagicMock()
            err.resp = MagicMock()
            err.resp.status = code
            assert is_retryable_error(err) is False, f"Status {code} should not be retryable"

    def test_timeout_string(self):
        """Should return True for timeout errors."""
        assert is_retryable_error(Exception("Connection timeout occurred")) is True

    def test_connection_reset_string(self):
        """Should return True for connection reset."""
        assert is_retryable_error(Exception("connection reset by peer")) is True

    def test_service_unavailable_string(self):
        """Should return True for service unavailable."""
        assert is_retryable_error(Exception("service unavailable")) is True

    def test_non_retryable_error(self):
        """Should return False for non-retryable errors."""
        assert is_retryable_error(ValueError("bad value")) is False

    def test_regular_exception(self):
        """Should return False for generic exception without patterns."""
        assert is_retryable_error(Exception("something else")) is False


# ── extract_retry_after Tests ───────────────────────────────────────

class TestExtractRetryAfter:
    """Tests for extract_retry_after function."""

    def test_rate_limit_error_with_retry_after(self):
        """Should extract retry_after from RateLimitError."""
        err = RateLimitError(retry_after=60)
        assert extract_retry_after(err) == 60

    def test_rate_limit_error_no_retry_after(self):
        """Should return None if no retry_after."""
        err = RateLimitError()
        assert extract_retry_after(err) is None

    def test_http_error_with_header(self):
        """Should extract Retry-After header from HTTP error."""
        err = MagicMock()
        err.resp = MagicMock()
        err.resp.headers = {'Retry-After': '30'}
        assert extract_retry_after(err) == 30

    def test_http_error_invalid_header(self):
        """Should return None for non-integer Retry-After."""
        err = MagicMock()
        err.resp = MagicMock()
        err.resp.headers = {'Retry-After': 'not-a-number'}
        assert extract_retry_after(err) is None

    def test_regular_exception(self):
        """Should return None for regular exceptions."""
        assert extract_retry_after(ValueError("test")) is None


# ── with_retry Tests ────────────────────────────────────────────────

class TestWithRetry:
    """Tests for with_retry decorator."""

    def test_success_no_retry(self):
        """Should return result on first success."""
        call_count = 0

        @with_retry(max_attempts=3, min_wait=0.01, max_wait=0.02)
        def succeed():
            nonlocal call_count
            call_count += 1
            return "ok"

        result = succeed()
        assert result == "ok"
        assert call_count == 1

    def test_retry_on_retryable_error(self):
        """Should retry on RetryableError and succeed."""
        call_count = 0

        @with_retry(max_attempts=3, min_wait=0.01, max_wait=0.02)
        def fail_then_succeed():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise RetryableError("transient", status_code=503)
            return "ok"

        result = fail_then_succeed()
        assert result == "ok"
        assert call_count == 3

    def test_no_retry_on_non_retryable(self):
        """Should not retry on non-retryable errors."""
        call_count = 0

        @with_retry(max_attempts=3, min_wait=0.01, max_wait=0.02)
        def fail_bad():
            nonlocal call_count
            call_count += 1
            raise ValueError("not retryable")

        with pytest.raises(ValueError, match="not retryable"):
            fail_bad()
        assert call_count == 1

    def test_max_attempts_exceeded(self):
        """Should raise after max attempts."""
        call_count = 0

        @with_retry(max_attempts=3, min_wait=0.01, max_wait=0.02)
        def always_fail():
            nonlocal call_count
            call_count += 1
            raise RetryableError("always failing", status_code=500)

        with pytest.raises(RetryableError, match="always failing"):
            always_fail()
        assert call_count == 3

    def test_retry_with_http_like_error(self):
        """Should retry on errors with retryable HTTP status."""
        call_count = 0

        @with_retry(max_attempts=3, min_wait=0.01, max_wait=0.02)
        def http_fail():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                err = MagicMock(spec=Exception)
                err.resp = MagicMock()
                err.resp.status = 429
                err.__class__ = Exception
                # Create a real exception that passes is_retryable_error
                real_err = Exception("rate limited")
                real_err.resp = MagicMock()
                real_err.resp.status = 429
                raise real_err
            return "recovered"

        result = http_fail()
        assert result == "recovered"
        assert call_count == 2

    def test_preserves_function_name(self):
        """Should preserve decorated function name."""
        @with_retry()
        def my_function():
            pass

        assert my_function.__name__ == "my_function"

    def test_retry_respects_retry_after(self):
        """Should use retry-after when present."""
        call_count = 0

        @with_retry(max_attempts=3, min_wait=0.01, max_wait=0.5)
        def rate_limited_call():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise RateLimitError(retry_after=0)  # 0 second wait
            return "ok"

        result = rate_limited_call()
        assert result == "ok"
        assert call_count == 2


# ── RateLimiter Tests ───────────────────────────────────────────────

class TestRateLimiter:
    """Tests for RateLimiter class."""

    def test_creation_defaults(self):
        """Should create with default values."""
        limiter = RateLimiter()
        assert limiter.calls_per_second == 10.0
        assert limiter.burst_size == 20

    def test_creation_custom(self):
        """Should accept custom values."""
        limiter = RateLimiter(calls_per_second=5.0, burst_size=10)
        assert limiter.calls_per_second == 5.0
        assert limiter.burst_size == 10

    def test_acquire_immediate(self):
        """Should acquire token immediately when available."""
        limiter = RateLimiter(calls_per_second=100, burst_size=10)
        start = time.monotonic()
        result = limiter.acquire()
        elapsed = time.monotonic() - start
        assert result is True
        assert elapsed < 0.1  # Should be near-instant

    def test_burst_capacity(self):
        """Should allow burst_size immediate calls."""
        limiter = RateLimiter(calls_per_second=1.0, burst_size=5)
        for i in range(5):
            result = limiter.acquire(timeout=0.01)
            assert result is True, f"Call {i+1} of burst should succeed"

    def test_timeout_when_exhausted(self):
        """Should timeout when tokens exhausted."""
        limiter = RateLimiter(calls_per_second=1.0, burst_size=1)
        # Use the one token
        limiter.acquire()
        # Next should timeout quickly
        result = limiter.acquire(timeout=0.05)
        assert result is False

    def test_token_refill(self):
        """Should refill tokens over time."""
        limiter = RateLimiter(calls_per_second=100.0, burst_size=2)
        # Exhaust tokens
        limiter.acquire()
        limiter.acquire()
        # Wait for refill
        time.sleep(0.05)  # Should refill ~5 tokens at 100/s
        result = limiter.acquire(timeout=0.01)
        assert result is True

    def test_context_manager(self):
        """Should work as context manager."""
        limiter = RateLimiter(calls_per_second=100, burst_size=10)
        with limiter:
            pass  # Should not raise

    def test_default_rate_limiter_exists(self):
        """Should have a default rate limiter configured."""
        assert default_rate_limiter is not None
        assert default_rate_limiter.calls_per_second == 8.0
        assert default_rate_limiter.burst_size == 15


# ── rate_limited decorator Tests ────────────────────────────────────

class TestRateLimitedDecorator:
    """Tests for rate_limited decorator."""

    def test_allows_call(self):
        """Should allow function call through."""
        limiter = RateLimiter(calls_per_second=100, burst_size=10)

        @rate_limited(limiter)
        def do_work():
            return "done"

        assert do_work() == "done"

    def test_preserves_return_value(self):
        """Should preserve function return value."""
        limiter = RateLimiter(calls_per_second=100, burst_size=10)

        @rate_limited(limiter)
        def calculate(x, y):
            return x + y

        assert calculate(3, 4) == 7

    def test_preserves_function_name(self):
        """Should preserve decorated function name."""
        limiter = RateLimiter(calls_per_second=100, burst_size=10)

        @rate_limited(limiter)
        def my_api_call():
            pass

        assert my_api_call.__name__ == "my_api_call"

    def test_uses_default_limiter(self):
        """Should use default limiter when none specified."""
        @rate_limited()
        def call_api():
            return "ok"

        assert call_api() == "ok"


# ── robust_api_call Tests ──────────────────────────────────────────

class TestRobustApiCall:
    """Tests for robust_api_call convenience function."""

    def test_success(self):
        """Should call function and return result."""
        limiter = RateLimiter(calls_per_second=100, burst_size=10)
        result = robust_api_call(lambda: "result", rate_limiter=limiter)
        assert result == "result"

    def test_with_args(self):
        """Should pass args to function."""
        limiter = RateLimiter(calls_per_second=100, burst_size=10)

        def add(a, b):
            return a + b

        result = robust_api_call(add, 3, 4, rate_limiter=limiter)
        assert result == 7

    def test_with_kwargs(self):
        """Should pass kwargs to function."""
        limiter = RateLimiter(calls_per_second=100, burst_size=10)

        def greet(name="world"):
            return f"hello {name}"

        result = robust_api_call(greet, name="test", rate_limiter=limiter)
        assert result == "hello test"

    def test_retries_on_failure(self):
        """Should retry on retryable errors."""
        limiter = RateLimiter(calls_per_second=100, burst_size=10)
        call_count = 0

        def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise RetryableError("transient", status_code=500)
            return "recovered"

        result = robust_api_call(flaky, max_attempts=3, rate_limiter=limiter)
        assert result == "recovered"
        assert call_count == 2

    def test_raises_on_persistent_failure(self):
        """Should raise after max attempts."""
        limiter = RateLimiter(calls_per_second=100, burst_size=10)

        def always_fail():
            raise RetryableError("always", status_code=500)

        with pytest.raises(RetryableError):
            robust_api_call(always_fail, max_attempts=2, rate_limiter=limiter)


# ── RETRYABLE_STATUS_CODES Tests ───────────────────────────────────

class TestRetryableStatusCodes:
    """Tests for RETRYABLE_STATUS_CODES constant."""

    def test_contains_expected_codes(self):
        """Should contain standard retryable codes."""
        expected = {408, 429, 500, 502, 503, 504}
        assert RETRYABLE_STATUS_CODES == expected

    def test_does_not_contain_client_errors(self):
        """Should not contain client errors."""
        for code in [400, 401, 403, 404, 405, 409, 422]:
            assert code not in RETRYABLE_STATUS_CODES

    def test_is_frozenset(self):
        """Should be immutable."""
        assert isinstance(RETRYABLE_STATUS_CODES, frozenset)
