from __future__ import annotations

import logging
import time

from redis.asyncio import Redis

from app.core.exceptions import RateLimitError

logger = logging.getLogger(__name__)


class SlidingWindowRateLimiter:
    """Redis sliding-window rate limiter.

    Limits per TRD Section 6:
    - Registered citizens: 10 issues/hour
    - Anonymous: 3 issues/hour/device
    - Login attempts: 5/minute/IP
    - Global API: 500/minute/IP
    """

    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    async def check_rate_limit(
        self,
        key: str,
        limit: int,
        window_seconds: int,
    ) -> None:
        """Check and increment the rate limit counter.

        Args:
            key: Rate limit key (e.g., "rate_limit:{user_id}:issues")
            limit: Max requests allowed in the window
            window_seconds: Size of the sliding window in seconds

        Raises:
            RateLimitError: If the rate limit is exceeded.
        """
        now = time.time()
        window_start = now - window_seconds

        pipe = self.redis.pipeline()

        # Remove entries outside the current window
        pipe.zremrangebyscore(key, 0, window_start)
        # Count remaining entries
        pipe.zcard(key)
        # Add the current request
        pipe.zadd(key, {f"{now}": now})
        # Set TTL on the key
        pipe.expire(key, window_seconds)

        results = await pipe.execute()
        current_count: int = results[1]

        if current_count >= limit:
            # Calculate retry-after
            oldest_entries = await self.redis.zrange(key, 0, 0, withscores=True)
            retry_after = int(window_seconds - (now - oldest_entries[0][1])) if oldest_entries else window_seconds

            window_str = f"{window_seconds // 3600}h" if window_seconds >= 3600 else f"{window_seconds // 60}min"
            raise RateLimitError(
                limit=limit,
                window=window_str,
                retry_after_seconds=max(retry_after, 1),
            )
