"""A tiny in-process cache for expensive, request-triggered ML computations
(Prophet fits, Isolation Forest passes) that are read-only and reproducible
for a given (parameters, current data) pair.

2026-08 audit: forecasting.py's single-item Prophet fit and both of
anomaly.py's Isolation Forest passes ran fresh on every single API request
-- correct (nothing was ever computed wrong), but wasteful under repeat
traffic: a manager opening the same forecast twice within a few minutes
pays the same several-second Prophet fit twice for an answer that could
not have changed. This module does not change what either algorithm
computes -- forecasting.py and anomaly.py are untouched -- it only avoids
recomputing an identical answer when nothing relevant has changed.

Invalidation is data-driven, not merely time-based: get_or_compute()'s key
is expected to include a cheap fingerprint of the data the computation
actually reads (e.g. MAX(id) from the table new orders land in) -- see
each call site in api.py. The instant a new order/ledger row exists, the
fingerprint changes and the next request recomputes for real. TTL and
max_entries below exist only to bound memory over the life of a long-running
process (the fingerprint changes on every single order, so a new key is
minted constantly) -- they are a safety net, not the primary invalidation
mechanism.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

DEFAULT_TTL_SECONDS = 300
DEFAULT_MAX_ENTRIES = 200


class TTLCache:
    def __init__(self, ttl_seconds: float = DEFAULT_TTL_SECONDS, max_entries: int = DEFAULT_MAX_ENTRIES):
        self.ttl = ttl_seconds
        self.max_entries = max_entries
        self._store: dict[Any, tuple[float, float, Any]] = {}  # key -> (expires_at, inserted_at, value)
        self._lock = threading.Lock()

    def get_or_compute(self, key: Any, compute: Callable[[], Any]) -> Any:
        now = time.monotonic()
        with self._lock:
            hit = self._store.get(key)
            if hit is not None and hit[0] > now:
                return hit[2]
        # Deliberately computed outside the lock: a Prophet fit or an
        # IsolationForest pass takes seconds, and holding a process-wide
        # lock for that long would serialize every console request behind
        # whichever one missed first. A duplicate concurrent miss for the
        # same key just means two requests each pay the real cost once
        # this one time -- correct, just not maximally efficient, which is
        # the right tradeoff for a handful of concurrent console users.
        value = compute()
        with self._lock:
            self._store[key] = (now + self.ttl, now, value)
            if len(self._store) > self.max_entries:
                self._evict_oldest_locked()
        return value

    def _evict_oldest_locked(self) -> None:
        oldest_key = min(self._store, key=lambda k: self._store[k][1])
        del self._store[oldest_key]

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)
