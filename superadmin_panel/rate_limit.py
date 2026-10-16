"""Minimal in-memory sliding-window rate limiter (Phase 13).

No Redis/external dependency - this SuperAdmin Control Plane is a single-
process Flask app for one operator's own customer base, and in-memory
state is a deliberate, documented trade-off appropriate at that scale
(see README.md's Production Deployment notes for when this would need to
move to a shared store, e.g. running multiple worker processes).
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

_lock = threading.Lock()
_attempts: dict[str, deque] = defaultdict(deque)


def is_rate_limited(key: str, *, max_attempts: int, window_seconds: float) -> bool:
    """Records this attempt and returns True if `key` has exceeded
    `max_attempts` within the trailing `window_seconds`."""
    now = time.monotonic()
    with _lock:
        bucket = _attempts[key]
        while bucket and now - bucket[0] > window_seconds:
            bucket.popleft()
        if len(bucket) >= max_attempts:
            return True
        bucket.append(now)
        return False


def forget_last_attempt(key: str) -> None:
    """Un-count the most recent attempt for `key` - for attempts that failed
    on our side, which shouldn't use up the caller's allowance."""
    with _lock:
        bucket = _attempts.get(key)
        if bucket:
            bucket.pop()


def reset(key: str) -> None:
    with _lock:
        _attempts.pop(key, None)


def reset_all() -> None:
    """Test hook."""
    with _lock:
        _attempts.clear()
