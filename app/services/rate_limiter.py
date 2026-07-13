from collections import deque
from datetime import datetime, timezone


class RateLimiter:
    """Простой in-memory rate limiter по ключу (chat_id)."""

    INACTIVE_TTL_SECONDS = 3600
    CLEANUP_EVERY_N = 100

    def __init__(self, max_requests: int = 10, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._requests: dict[str, deque[float]] = {}
        self._request_count = 0

    def _cleanup_inactive(self, now: float) -> None:
        stale_keys = []
        for key, bucket in self._requests.items():
            if not bucket or now - bucket[-1] > self.INACTIVE_TTL_SECONDS:
                stale_keys.append(key)
        for key in stale_keys:
            del self._requests[key]

    def is_allowed(self, key: str) -> bool:
        now = datetime.now(timezone.utc).timestamp()
        bucket = self._requests.get(key)
        if bucket is None:
            bucket = deque()
            self._requests[key] = bucket

        while bucket and now - bucket[0] > self.window_seconds:
            bucket.popleft()

        if len(bucket) >= self.max_requests:
            return False

        bucket.append(now)

        self._request_count += 1
        if self._request_count % self.CLEANUP_EVERY_N == 0:
            self._cleanup_inactive(now)

        return True
