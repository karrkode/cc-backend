"""A small in-memory rate limiter for the login and register endpoints.

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


auth_limiter = SlidingWindowLimiter()


def limit_auth_attempts(request: Request):
    # nginx sets X-Forwarded-For; uvicorn's --proxy-headers turns it into request.client.host
    client = request.client.host if request.client else "unknown"
    if not auth_limiter.hit(f"{request.url.path}|{client}", get_settings().login_attempts_per_minute):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts. Try again in a minute.")
