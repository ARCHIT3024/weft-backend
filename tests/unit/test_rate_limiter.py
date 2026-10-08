"""Behavioural tests for `app/core/rate_limiter.py` — the sliding window.

This module was at 0% coverage. It is the only thing standing between the API
and the abuse budgets in TRD Section 6 (10 issues/hour, 5 logins/minute/IP,
500 requests/minute/IP), so "it imports cleanly" was not much of a guarantee.

Redis is replaced by `FakeRedis` below: a small, honest in-memory implementation
of exactly the five commands the limiter uses (`zremrangebyscore`, `zcard`,
`zadd`, `expire`, `zrange`) with their real semantics — sorted-set members are
unique, scores order them, and pipelined commands return one result each. That
makes these tests behavioural rather than call-shaped: the limiter's own
arithmetic decides whether a request is allowed. The fake also records every
command, so the interaction with Redis can be asserted where it matters.

`time.time` is replaced by an explicit `FakeClock` for every test. That is not
only for determinism: the limiter derives its sorted-set *member* from the
timestamp, so on a coarse clock several requests share a member and overwrite
one another. See `test_requests_sharing_a_timestamp_collapse_into_one_entry`.

No test sleeps, and no test needs a real Redis.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest

from app.core.exceptions import RateLimitError
from app.core.rate_limiter import SlidingWindowRateLimiter

KEY = "rate_limit:user-1:issues"
OTHER_KEY = "rate_limit:user-2:issues"


# ── Redis double ────────────────────────────────────────────────────────


class FakePipeline:
    """Queues commands and applies them on `execute()`, like redis-py."""

    def __init__(self, redis: FakeRedis) -> None:
        self._redis = redis
        self._queued: list[tuple[str, tuple[Any, ...]]] = []

    def zremrangebyscore(self, key: str, min_score: float, max_score: float) -> FakePipeline:
        self._queued.append(("zremrangebyscore", (key, min_score, max_score)))
        return self

    def zcard(self, key: str) -> FakePipeline:
        self._queued.append(("zcard", (key,)))
        return self

    def zadd(self, key: str, mapping: dict[str, float]) -> FakePipeline:
        self._queued.append(("zadd", (key, mapping)))
        return self

    def expire(self, key: str, seconds: int) -> FakePipeline:
        self._queued.append(("expire", (key, seconds)))
        return self

    async def execute(self) -> list[Any]:
        results: list[Any] = []
        for name, args in self._queued:
            self._redis.commands.append((name, *args))
            results.append(getattr(self._redis, f"_{name}")(*args))
        self._queued.clear()
        return results


class FakeRedis:
    """In-memory stand-in for the subset of Redis the limiter uses."""

    def __init__(self) -> None:
        self.sets: dict[str, dict[str, float]] = {}
        self.ttls: dict[str, int] = {}
        self.commands: list[tuple[Any, ...]] = []

    def pipeline(self) -> FakePipeline:
        return FakePipeline(self)

    # -- command implementations --------------------------------------

    def _zremrangebyscore(self, key: str, min_score: float, max_score: float) -> int:
        members = self.sets.setdefault(key, {})
        doomed = [m for m, score in members.items() if min_score <= score <= max_score]
        for member in doomed:
            del members[member]
        return len(doomed)

    def _zcard(self, key: str) -> int:
        return len(self.sets.get(key, {}))

    def _zadd(self, key: str, mapping: dict[str, float]) -> int:
        members = self.sets.setdefault(key, {})
        added = sum(1 for member in mapping if member not in members)
        members.update(mapping)
        return added

    def _expire(self, key: str, seconds: int) -> bool:
        self.ttls[key] = seconds
        return True

    async def zrange(self, key: str, start: int, end: int, withscores: bool = False) -> list[Any]:
        ordered = sorted(self.sets.get(key, {}).items(), key=lambda item: item[1])
        stop = len(ordered) if end == -1 else end + 1
        window = ordered[start:stop]
        return window if withscores else [member for member, _ in window]


class FakeClock:
    """A monotonic clock that advances by `step` on each read.

    `step` models a fine-grained system clock: consecutive requests get
    distinct timestamps, which is what the limiter needs for its members to be
    distinct. `set()` jumps the clock forward to age entries out of a window.
    """

    def __init__(self, start: float = 1_000.0, step: float = 0.001) -> None:
        self.now = start
        self.step = step

    def __call__(self) -> float:
        value = self.now
        self.now += self.step
        return value

    def set(self, when: float) -> None:
        self.now = when


@pytest.fixture
def redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def limiter(redis: FakeRedis) -> SlidingWindowRateLimiter:
    return SlidingWindowRateLimiter(redis)


@pytest.fixture
def clock() -> Iterator[FakeClock]:
    fake = FakeClock()
    with patch("app.core.rate_limiter.time.time", fake):
        yield fake


# ── The window itself ───────────────────────────────────────────────────


async def test_requests_below_the_limit_are_allowed(limiter: SlidingWindowRateLimiter, clock: FakeClock) -> None:
    for _ in range(5):
        await limiter.check_rate_limit(KEY, limit=5, window_seconds=60)


async def test_the_request_that_crosses_the_limit_raises(limiter: SlidingWindowRateLimiter, clock: FakeClock) -> None:
    """Exactly `limit` requests get through; number `limit + 1` is refused."""
    for _ in range(5):
        await limiter.check_rate_limit(KEY, limit=5, window_seconds=60)

    with pytest.raises(RateLimitError):
        await limiter.check_rate_limit(KEY, limit=5, window_seconds=60)


async def test_a_limit_of_one_allows_exactly_one_request(limiter: SlidingWindowRateLimiter, clock: FakeClock) -> None:
    await limiter.check_rate_limit(KEY, limit=1, window_seconds=60)

    with pytest.raises(RateLimitError):
        await limiter.check_rate_limit(KEY, limit=1, window_seconds=60)


async def test_distinct_keys_are_counted_independently(limiter: SlidingWindowRateLimiter, clock: FakeClock) -> None:
    """One user exhausting their budget must not lock out anybody else."""
    for _ in range(3):
        await limiter.check_rate_limit(KEY, limit=3, window_seconds=60)

    with pytest.raises(RateLimitError):
        await limiter.check_rate_limit(KEY, limit=3, window_seconds=60)

    await limiter.check_rate_limit(OTHER_KEY, limit=3, window_seconds=60)


async def test_the_window_slides_so_old_requests_stop_counting(
    limiter: SlidingWindowRateLimiter,
    redis: FakeRedis,
    clock: FakeClock,
) -> None:
    """Sliding, not fixed: once the earlier burst ages past `window_seconds`
    the caller is served again."""
    for _ in range(3):
        await limiter.check_rate_limit(KEY, limit=3, window_seconds=60)
    with pytest.raises(RateLimitError):
        await limiter.check_rate_limit(KEY, limit=3, window_seconds=60)

    clock.set(1_061.0)
    await limiter.check_rate_limit(KEY, limit=3, window_seconds=60)

    assert all(score >= 1_061.0 for score in redis.sets[KEY].values())


async def test_requests_still_inside_the_window_are_not_evicted(
    limiter: SlidingWindowRateLimiter,
    clock: FakeClock,
) -> None:
    for _ in range(3):
        await limiter.check_rate_limit(KEY, limit=3, window_seconds=60)

    clock.set(1_059.0)

    with pytest.raises(RateLimitError):
        await limiter.check_rate_limit(KEY, limit=3, window_seconds=60)


async def test_eviction_range_is_everything_older_than_the_window(
    limiter: SlidingWindowRateLimiter,
    redis: FakeRedis,
    clock: FakeClock,
) -> None:
    clock.set(5_000.0)

    await limiter.check_rate_limit(KEY, limit=10, window_seconds=3600)

    assert ("zremrangebyscore", KEY, 0, 5_000.0 - 3600) in redis.commands


# ── Key expiry ──────────────────────────────────────────────────────────


async def test_ttl_is_set_on_the_window_key(
    limiter: SlidingWindowRateLimiter,
    redis: FakeRedis,
    clock: FakeClock,
) -> None:
    """Without an `EXPIRE`, every key a caller ever touches leaks in Redis."""
    await limiter.check_rate_limit(KEY, limit=10, window_seconds=3600)

    assert redis.ttls[KEY] == 3600
    assert ("expire", KEY, 3600) in redis.commands


async def test_ttl_is_refreshed_on_every_request(
    limiter: SlidingWindowRateLimiter,
    redis: FakeRedis,
    clock: FakeClock,
) -> None:
    for _ in range(3):
        await limiter.check_rate_limit(KEY, limit=10, window_seconds=60)

    assert [cmd for cmd in redis.commands if cmd[0] == "expire"] == [("expire", KEY, 60)] * 3


async def test_all_four_commands_are_issued_in_one_pipeline(
    limiter: SlidingWindowRateLimiter,
    redis: FakeRedis,
    clock: FakeClock,
) -> None:
    """Batching keeps the read-modify-write to a single round trip."""
    await limiter.check_rate_limit(KEY, limit=10, window_seconds=60)

    assert [cmd[0] for cmd in redis.commands] == ["zremrangebyscore", "zcard", "zadd", "expire"]


# ── The error the caller sees ───────────────────────────────────────────


async def test_rate_limit_error_carries_the_contracted_429_payload(
    limiter: SlidingWindowRateLimiter,
    clock: FakeClock,
) -> None:
    clock.set(2_000.0)
    await limiter.check_rate_limit(KEY, limit=1, window_seconds=3600)

    clock.set(2_600.0)
    with pytest.raises(RateLimitError) as exc_info:
        await limiter.check_rate_limit(KEY, limit=1, window_seconds=3600)

    error = exc_info.value
    assert error.status_code == 429
    assert error.code == "RATE_LIMIT_EXCEEDED"
    assert error.details["limit"] == 1
    assert error.details["window"] == "1h"
    # 3600s window, oldest hit 600s ago -> 3000s until it ages out.
    assert error.details["retry_after_seconds"] == 3000


async def test_retry_after_is_never_below_one_second(limiter: SlidingWindowRateLimiter, clock: FakeClock) -> None:
    """A `Retry-After: 0` invites an immediate retry storm."""
    clock.set(2_000.0)
    await limiter.check_rate_limit(KEY, limit=1, window_seconds=60)

    clock.set(2_059.9)
    with pytest.raises(RateLimitError) as exc_info:
        await limiter.check_rate_limit(KEY, limit=1, window_seconds=60)

    assert exc_info.value.details["retry_after_seconds"] == 1


@pytest.mark.parametrize(
    ("window_seconds", "label"),
    [(60, "1min"), (300, "5min"), (3600, "1h"), (86400, "24h")],
)
async def test_window_is_described_in_human_units(
    limiter: SlidingWindowRateLimiter,
    clock: FakeClock,
    window_seconds: int,
    label: str,
) -> None:
    await limiter.check_rate_limit(KEY, limit=1, window_seconds=window_seconds)

    with pytest.raises(RateLimitError) as exc_info:
        await limiter.check_rate_limit(KEY, limit=1, window_seconds=window_seconds)

    assert exc_info.value.details["window"] == label
    assert label in exc_info.value.error_message


# ── Documented current behaviour — both flagged in the report ───────────


async def test_requests_sharing_a_timestamp_collapse_into_one_entry(
    limiter: SlidingWindowRateLimiter,
    redis: FakeRedis,
) -> None:
    """BUG PIN: the sorted-set member is `str(time.time())`, so requests that
    read the same clock value overwrite each other instead of counting
    separately — the limit is then silently unenforceable.

    The clock is frozen here to make it deterministic, but this is not a
    contrived condition. `time.get_clock_info("time").resolution` is 0.015625
    on Windows, where 2000 back-to-back reads return a *single* distinct value;
    an earlier draft of this file reproduced the collapse against the real
    clock. Linux clocks are far finer, but two coroutines reading one value is
    still possible and nothing in the design forbids it.

    The fix is a unique member (e.g. `uuid4().hex`, or `f"{now}:{token_hex(8)}"`)
    with `now` kept as the score. When that lands, this test should assert 20
    entries and a `RateLimitError` instead.
    """
    with patch("app.core.rate_limiter.time.time", return_value=9_000.0):
        for _ in range(20):
            # 20 requests against a limit of 5, and not one of them is refused.
            await limiter.check_rate_limit(KEY, limit=5, window_seconds=60)

    assert len(redis.sets[KEY]) == 1


async def test_a_refused_request_is_still_recorded_in_the_window(
    limiter: SlidingWindowRateLimiter,
    redis: FakeRedis,
    clock: FakeClock,
) -> None:
    """BUG PIN: `zadd` runs in the same pipeline as the count, before the limit
    check, so a *rejected* request is recorded too.

    A client that keeps hammering after its 429 keeps re-arming the window and
    extends its own lockout indefinitely. Flagged in the report; if the module
    changes to record only served requests, invert this expectation.
    """
    clock.set(7_000.0)
    await limiter.check_rate_limit(KEY, limit=1, window_seconds=60)

    clock.set(7_030.0)
    with pytest.raises(RateLimitError):
        await limiter.check_rate_limit(KEY, limit=1, window_seconds=60)

    assert len(redis.sets[KEY]) == 2
