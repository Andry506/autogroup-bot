from collections import deque
from datetime import datetime, timezone


class RateLimiter:
    """Простой in-memory rate limiter по ключу (chat_id)."""

    def __init__(self, max_requests: int = 10, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._requests: dict[str, deque[float]] = {}

    def is_allowed(self, key: str) -> bool:
        now = datetime.now(timezone.utc).timestamp()
        bucket = self._requests.setdefault(key, deque())

        while bucket and now - bucket[0] > self.window_seconds:
            bucket.popleft()

        if len(bucket) >= self.max_requests:
            return False

        bucket.append(now)
        return True
