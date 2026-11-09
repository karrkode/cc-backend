"""A small in-memory rate limiter for the login and register endpoints, and (CC-191) a count of
failed sign-ins per account.

Fine for one API server. With several servers behind a load balancer the counts
would need to live somewhere shared (Redis / ElastiCache) instead.
"""
import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request, status

from .config import get_settings


class SlidingWindowLimiter:
    def __init__(self, window_seconds: float = 60.0):
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int) -> bool:
        """Record a request; False if this key is over its limit for the window."""
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            return True

    def reset(self):
        with self._lock:
            self._hits.clear()


class FailureCounter:
    """CC-191: failures per key (an account's email) in a sliding window. Unlike the limiter above,
    only failures count, and they're recorded after the attempt."""

    PRUNE_AT = 10_000  # keys; past this, forget the ones whose failures have all expired

    def __init__(self, window_seconds: float):
        self.window = window_seconds
        self._fails: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> deque[float] | None:
        q = self._fails.get(key)
        while q and now - q[0] > self.window:
            q.popleft()
        return q

    def blocked(self, key: str, limit: int) -> bool:
        with self._lock:
            q = self._recent(key, time.monotonic())
            return q is not None and len(q) >= limit

    def record(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            if len(self._fails) >= self.PRUNE_AT:
                for k in [k for k in self._fails if not self._recent(k, now)]:
                    del self._fails[k]
            self._fails.setdefault(key, deque()).append(now)

    def clear(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)

    def reset(self):
        with self._lock:
            self._fails.clear()


auth_limiter = SlidingWindowLimiter()
# CC-191: keyed by the email typed, whether or not it has an account, so the reply is the same either way
login_failures = FailureCounter(get_settings().login_failure_window_minutes * 60)


def limit_auth_attempts(request: Request):
    # nginx sets X-Forwarded-For; uvicorn's --proxy-headers turns it into request.client.host
    client = request.client.host if request.client else "unknown"
    if not auth_limiter.hit(f"{request.url.path}|{client}", get_settings().login_attempts_per_minute):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts. Try again in a minute.")
