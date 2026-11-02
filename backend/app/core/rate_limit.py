"""Small per-process request limiter for protected interactive endpoints.

Production deployment should put the same policy in a shared store such as Redis so
multiple API workers share counters. This keeps local and single-worker deployments safe.
"""

import time
from collections import defaultdict, deque

from fastapi import HTTPException, status


class SlidingWindowLimiter:
    def __init__(self, maximum: int = 20, window_seconds: int = 60):
        self.maximum = maximum
        self.window_seconds = window_seconds
        self._calls: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> None:
        now = time.monotonic()
        calls = self._calls[key]
        cutoff = now - self.window_seconds
        while calls and calls[0] <= cutoff:
            calls.popleft()
        if len(calls) >= self.maximum:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests; try again in a minute",
                headers={"Retry-After": str(self.window_seconds)},
            )
        calls.append(now)


interactive_limiter = SlidingWindowLimiter()
# Suggestions are automatic, so they don't use up the customer's own message allowance.
suggestion_limiter = SlidingWindowLimiter()
