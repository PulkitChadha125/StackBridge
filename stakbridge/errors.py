"""Errors returned by the API, and the sign-in rate limiter."""

import threading
import time
from collections import defaultdict, deque


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.details: dict | None = None  # extra facts for the page, e.g. the client limit
        self.retryable = False  # forgot password: the same step can be tried again


class RateLimiter:
    """Sliding one-minute window per key."""

    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] >= 60:
                hits.popleft()
            if len(hits) >= self.per_minute:
                return False
            hits.append(now)
            return True
