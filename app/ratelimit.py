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
    def __init__(self, limits: dict[str, tuple[int, float]] | None = None) -> None:
        self.limits = dict(limits or DEFAULT_LIMITS)
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self.refused: dict[str, int] = defaultdict(int)
        self._pruned = time.monotonic()

    def _prune(self, now: float) -> None:
        """Drop keys with no hit inside their window (one per email ever
        tried, otherwise kept forever). Runs at most once a minute."""
        if now - self._pruned < 60:
            return
        self._pruned = now
        longest = max(w for _, w in self.limits.values())
        for k in [k for k, q in self._hits.items() if not q or now - q[-1] > longest]:
            del self._hits[k]

    def hit(self, bucket: str, key: str, limit: int, window: float) -> None:
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            q = self._hits[f"{bucket}:{key}"]
            while q and now - q[0] > window:
                q.popleft()
            if len(q) >= limit:
                self.refused[bucket] += 1
                raise TooMany(f"too many attempts; wait {int(window - (now - q[0])) + 1} seconds and try again")
            q.append(now)


# (attempts, window in seconds). The per-email login limit is the guessing
# defence; the per-address limits only stop one machine spraying, and are set
# well above a whole event's size, because a venue NAT or a reverse proxy
# missing from FORWARDED_ALLOW_IPS makes every participant one address.
DEFAULT_LIMITS = {
    "login": (10, 300.0),         # per email: guessing one password
    "login-ip": (600, 300.0),     # per address: spraying many accounts
    "signup": (300, 3600.0),      # per address
    "vote": (30, 60.0),           # per account
    "comment": (10, 300.0),       # per account
}


def parse_limits(spec: str | None) -> dict[str, tuple[int, float]]:
    """PLUMB_RATE_LIMITS, e.g. "signup=1000/3600,login-ip=2000/300": attempts
    per window in seconds, overriding the defaults for the buckets named."""
    limits = dict(DEFAULT_LIMITS)
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            name, value = part.split("=", 1)
            attempts, window = value.split("/", 1)
            name, attempts_n, window_s = name.strip(), int(attempts), float(window)
        except ValueError as exc:
            raise ValueError(f"PLUMB_RATE_LIMITS: cannot read {part!r}; write bucket=attempts/seconds") from exc
        if name not in DEFAULT_LIMITS:
            raise ValueError(f"PLUMB_RATE_LIMITS: unknown bucket {name!r}; known: {', '.join(DEFAULT_LIMITS)}")
        if attempts_n < 1 or window_s <= 0:
            raise ValueError(f"PLUMB_RATE_LIMITS: {name} needs at least one attempt in a positive window")
        limits[name] = (attempts_n, window_s)
    return limits


LIMITS = dict(DEFAULT_LIMITS)


def check(limiter: RateLimiter, bucket: str, *keys: str | None) -> None:
    limit, window = limiter.limits.get(bucket, LIMITS[bucket])
    for key in keys:
        if key:
            limiter.hit(bucket, key, limit, window)
