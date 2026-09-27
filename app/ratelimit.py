"""In-process sliding-window rate limits.

Plumb runs as one process, so a dictionary is the whole implementation. The
limits reset on restart, which is acceptable for their job: slowing down
password guessing, vote flooding and comment spam, not billing.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from . import domain


class TooMany(domain.DomainError):
    status = 429


class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self.refused: dict[str, int] = defaultdict(int)

    def hit(self, bucket: str, key: str, limit: int, window: float) -> None:
        now = time.monotonic()
        with self._lock:
            q = self._hits[f"{bucket}:{key}"]
            while q and now - q[0] > window:
                q.popleft()
            if len(q) >= limit:
                self.refused[bucket] += 1
                raise TooMany(f"too many attempts; wait {int(window - (now - q[0])) + 1} seconds and try again")
            q.append(now)


LIMITS = {
    "login": (10, 300.0),         # per email: guessing one password
    "login-ip": (60, 300.0),      # per address: spraying many accounts
    "signup": (30, 3600.0),       # per address; generous, since a venue shares one NAT
    "vote": (30, 60.0),           # per account
    "comment": (10, 300.0),       # per account
}


def check(limiter: RateLimiter, bucket: str, *keys: str | None) -> None:
    limit, window = LIMITS[bucket]
    for key in keys:
        if key:
            limiter.hit(bucket, key, limit, window)
