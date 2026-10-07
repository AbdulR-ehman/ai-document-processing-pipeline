"""Simple in-memory rate limiting and login lockout.

NOTE / LIMITATION: this lives in process memory. It is reset when the server
restarts and is not shared between multiple worker processes. That is an
acceptable trade-off for a single-process portfolio deployment; a production
system would use a shared store (e.g. Redis) behind the same tiny interface.
"""

from __future__ import annotations

import re
import threading
import time
from collections import defaultdict, deque

_UNIT_SECONDS = {"second": 1, "minute": 60, "hour": 3600}
_SPEC_RE = re.compile(r"^\s*(\d+)\s*/\s*(second|minute|hour)s?\s*$", re.IGNORECASE)


class InvalidLimitSpec(ValueError):
    """Raised when a rate limit string cannot be parsed."""


def parse_limit(spec: str) -> tuple[int, int]:
    """Parse ``"10/minute"`` into ``(10, 60)``."""
    match = _SPEC_RE.match(spec or "")
    if not match:
        raise InvalidLimitSpec(f"Invalid rate limit spec: {spec!r}")
    return int(match.group(1)), _UNIT_SECONDS[match.group(2).lower()]


class RateLimiter:
    """Sliding-window limiter keyed by an arbitrary string."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str, spec: str) -> tuple[bool, int]:
        """Register a hit. Returns ``(allowed, retry_after_seconds)``."""
        limit, window = parse_limit(spec)
        now = time.monotonic()
        cutoff = now - window
        with self._lock:
            bucket = self._hits[key]
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                retry_after = max(1, int(bucket[0] + window - now) + 1)
                return False, retry_after
            bucket.append(now)
            return True, 0

    def reset(self, key: str | None = None) -> None:
        """Clear one key or everything (used by tests)."""
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)


class LoginThrottle:
    """Failed-login backoff per account identifier *and* per client IP."""

    def __init__(self, max_failures: int = 5, lockout_seconds: int = 300) -> None:
        self.max_failures = max_failures
        self.lockout_seconds = lockout_seconds
        self._failures: dict[str, list[float]] = defaultdict(list)
        self._locked_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def is_locked(self, *identifiers: str) -> tuple[bool, int]:
        """Return ``(locked, seconds_remaining)`` for any of the identifiers."""
        now = time.monotonic()
        with self._lock:
            remaining = 0.0
            for identifier in identifiers:
                until = self._locked_until.get(identifier, 0.0)
                if until > now:
                    remaining = max(remaining, until - now)
        if remaining > 0:
            return True, int(remaining) + 1
        return False, 0

    def configure(self, *, max_failures: int, lockout_seconds: int) -> None:
        """Apply application settings (called once at startup)."""
        with self._lock:
            self.max_failures = max(1, int(max_failures))
            self.lockout_seconds = max(1, int(lockout_seconds))

    def record_failure(self, *identifiers: str) -> None:
        """Count a failed attempt and lock out when the threshold is hit."""
        now = time.monotonic()
        window_start = now - self.lockout_seconds
        with self._lock:
            for identifier in identifiers:
                attempts = [t for t in self._failures.get(identifier, []) if t > window_start]
                attempts.append(now)
                self._failures[identifier] = attempts
                if len(attempts) >= self.max_failures:
                    self._locked_until[identifier] = now + self.lockout_seconds
                    self._failures[identifier] = []

    def reset(self, *identifiers: str) -> None:
        """Clear counters after a successful login."""
        with self._lock:
            for identifier in identifiers:
                self._failures.pop(identifier, None)
                self._locked_until.pop(identifier, None)

    def clear(self) -> None:
        with self._lock:
            self._failures.clear()
            self._locked_until.clear()


# Process-wide singletons used by the API layer.
rate_limiter = RateLimiter()
login_throttle = LoginThrottle()
