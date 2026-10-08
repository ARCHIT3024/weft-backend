"""Tests for the login rate-limit *wiring* — `POST /v1/auth/login` at 5/min/IP.

`tests/unit/test_rate_limiter.py` already proves the sliding window counts
correctly. What was missing is the thing that makes it matter: until now
`SlidingWindowRateLimiter` was constructed nowhere, so the budget in TRD
Section 6 existed only on paper and login had no brute-force protection at all.
These tests pin the connection — that the limiter is attached to the login
route, keyed per client IP, and that a Redis outage cannot lock users out.

Everything runs through the real ASGI app so the assertion is about the wiring
rather than about a function called in isolation:

* Redis is a `FakeRedis` parked on `app.state.redis`, the same slot the lifespan
  handler fills in production. It implements the five commands the limiter uses
  with their real semantics, following the fake in `test_rate_limiter.py`.
* `authenticate_user` is replaced with one that always raises the real 401. That
  keeps bcrypt and Postgres out of the loop, and models the case that actually
  matters: an attacker guessing passwords. A 401 here means "the limiter let it
  reach the handler"; a 429 means "the limiter stopped it".
* `time.time` is a `FakeClock`. Not only for determinism — the limiter derives
  its sorted-set *member* from the timestamp, and Windows' 15.6 ms clock
  resolution makes back-to-back requests collapse onto one member (the bug
  pinned in `test_rate_limiter.py`). Without an injected clock the over-limit
  test would flake by platform.

No test needs a database, a Redis, or a sleep.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.exceptions import UnauthorizedError
from app.database import get_db
from app.dependencies import LOGIN_RATE_LIMIT, client_ip
from app.main import create_app

LOGIN_URL = "/v1/auth/login"
CREDENTIALS = {"email": "citizen@example.com", "password": "hunter2-hunter2"}


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
            results.append(getattr(self._redis, f"_{name}")(*args))
        self._queued.clear()
        return results


class FakeRedis:
    """In-memory stand-in for the subset of Redis the limiter uses."""

    def __init__(self) -> None:
        self.sets: dict[str, dict[str, float]] = {}
        self.ttls: dict[str, int] = {}

    def pipeline(self) -> FakePipeline:
        return FakePipeline(self)

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


class BrokenRedis:
    """A Redis that is reachable enough to be handed out and useless after that.

    Models the failure the fail-open path exists for: the pool was built at
    startup, the server has since gone away, and every command raises.
    """

    def __init__(self) -> None:
        self.attempts = 0

    def pipeline(self) -> FakePipeline:
        self.attempts += 1
        raise ConnectionError("Error 111 connecting to localhost:6379. Connection refused.")


class FakeClock:
    """A monotonic clock that advances by `step` on each read."""

    def __init__(self, start: float = 1_000.0, step: float = 0.001) -> None:
        self.now = start
        self.step = step

    def __call__(self) -> float:
        value = self.now
        self.now += self.step
        return value

    def set(self, when: float) -> None:
        self.now = when


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def clock() -> Iterator[FakeClock]:
    fake = FakeClock()
    with patch("app.core.rate_limiter.time.time", fake):
        yield fake


@pytest.fixture
def rejecting_auth() -> Iterator[AsyncMock]:
    """Replace credential checking with a guaranteed 401.

    The route is reached or it is not; that is the only thing these tests read
    out of the response. Patching here also keeps bcrypt (~100 ms a call) and
    Postgres out of a unit test.
    """
    failing = AsyncMock(side_effect=UnauthorizedError("Invalid email or password"))
    with patch("app.services.auth_service.authenticate_user", failing):
        yield failing


def _build_client(redis_client: object | None) -> AsyncClient:
    """An ASGI client for the real app, with `redis_client` in the lifespan's slot."""
    app = create_app()
    # The slot `app.main.lifespan` fills in production. httpx's ASGI transport
    # does not run lifespan, so the wiring is reproduced explicitly.
    app.state.redis = redis_client
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def client(redis: FakeRedis, rejecting_auth: AsyncMock) -> AsyncIterator[AsyncClient]:
    async with _build_client(redis) as ac:
        yield ac


# ── Under the limit ─────────────────────────────────────────────────────


async def test_attempts_under_the_limit_reach_the_handler(client: AsyncClient, clock: FakeClock) -> None:
    """All five of the budgeted attempts get a real answer, not a 429."""
    for _ in range(LOGIN_RATE_LIMIT):
        response = await client.post(LOGIN_URL, json=CREDENTIALS)
        assert response.status_code == 401


def test_the_limit_is_five_per_minute() -> None:
    """Guards the constant itself against a silent edit — TRD Section 6."""
    assert LOGIN_RATE_LIMIT == 5


async def test_attempts_are_counted_under_a_per_ip_key(
    client: AsyncClient,
    redis: FakeRedis,
    clock: FakeClock,
) -> None:
    """One noisy address must not spend anybody else's budget."""
    await client.post(LOGIN_URL, json=CREDENTIALS)

    assert list(redis.sets) == ["rate_limit:login:127.0.0.1"]


# ── Over the limit ──────────────────────────────────────────────────────


async def test_the_sixth_attempt_in_a_minute_is_refused(client: AsyncClient, clock: FakeClock) -> None:
    for _ in range(LOGIN_RATE_LIMIT):
        assert (await client.post(LOGIN_URL, json=CREDENTIALS)).status_code == 401

    response = await client.post(LOGIN_URL, json=CREDENTIALS)

    assert response.status_code == 429


async def test_the_429_carries_the_contracted_error_body(client: AsyncClient, clock: FakeClock) -> None:
    for _ in range(LOGIN_RATE_LIMIT + 1):
        response = await client.post(LOGIN_URL, json=CREDENTIALS)

    error = response.json()["error"]
    assert error["code"] == "RATE_LIMIT_EXCEEDED"
    assert error["details"]["limit"] == LOGIN_RATE_LIMIT
    assert error["details"]["window"] == "1min"
    assert error["details"]["retry_after_seconds"] >= 1


async def test_a_refused_attempt_never_reaches_credential_checking(
    client: AsyncClient,
    rejecting_auth: AsyncMock,
    clock: FakeClock,
) -> None:
    """The point of a brute-force cap: past the limit, no password is verified."""
    for _ in range(LOGIN_RATE_LIMIT + 1):
        await client.post(LOGIN_URL, json=CREDENTIALS)

    assert rejecting_auth.await_count == LOGIN_RATE_LIMIT


async def test_the_window_slides_so_the_caller_is_served_again(client: AsyncClient, clock: FakeClock) -> None:
    for _ in range(LOGIN_RATE_LIMIT + 1):
        await client.post(LOGIN_URL, json=CREDENTIALS)

    clock.set(1_061.0)

    assert (await client.post(LOGIN_URL, json=CREDENTIALS)).status_code == 401


# ── Redis down: fail open ───────────────────────────────────────────────


async def test_login_still_works_when_redis_commands_fail(rejecting_auth: AsyncMock, clock: FakeClock) -> None:
    """A cache outage must not become an authentication outage.

    Deliberate availability-over-strictness tradeoff, mirroring the AI fallback
    in DECISIONS.md D-2. If someone ever decides the opposite, this test is
    where the decision has to be argued.
    """
    broken = BrokenRedis()
    async with _build_client(broken) as client:
        for _ in range(LOGIN_RATE_LIMIT + 5):
            assert (await client.post(LOGIN_URL, json=CREDENTIALS)).status_code == 401

    assert broken.attempts == LOGIN_RATE_LIMIT + 5, "each request should still have tried Redis"


async def test_login_still_works_when_there_is_no_redis_client(rejecting_auth: AsyncMock) -> None:
    """`app.state.redis` is `None` when the pool could not be built at startup."""
    async with _build_client(None) as client:
        for _ in range(LOGIN_RATE_LIMIT + 5):
            assert (await client.post(LOGIN_URL, json=CREDENTIALS)).status_code == 401


async def test_a_redis_outage_is_logged_not_swallowed(
    rejecting_auth: AsyncMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Failing open silently would make the gap invisible in production."""
    with caplog.at_level("WARNING", logger="app.dependencies"):
        async with _build_client(BrokenRedis()) as client:
            await client.post(LOGIN_URL, json=CREDENTIALS)

    assert any("fail-open" in record.message for record in caplog.records)


# ── Client identity ─────────────────────────────────────────────────────


def test_client_ip_falls_back_when_the_transport_has_no_peer() -> None:
    """Some ASGI servers omit `client`; a `None` deref there would 500 login."""

    class _NoPeerRequest:
        client = None

    assert client_ip(_NoPeerRequest()) == "unknown"


# ── The limit is scoped to login only ───────────────────────────────────


async def test_other_auth_routes_are_not_rate_limited(client: AsyncClient, clock: FakeClock) -> None:
    """Only task 1.3's budget is wired; the other three attach to endpoints that
    do not exist yet and were deliberately left alone."""
    for _ in range(LOGIN_RATE_LIMIT + 3):
        assert (await client.post("/v1/auth/oauth/google")).status_code == 200
