"""Tests for the issue-submission rate-limit *wiring* — `POST /v1/issues`.

TRD Section 6: 10 submissions/hour for a signed-in user, 3/hour per client IP
for an anonymous one. `tests/unit/test_rate_limiter.py` proves the sliding
window counts correctly; these pin the connection — that the limit is attached
to the submission route, keyed per user or per IP as the caller deserves, that
a refusal never reaches the handler, that the 429 carries `Retry-After`, and
that a Redis outage cannot stop citizens reporting hazards.

Same harness as `test_login_rate_limit.py`, whose Redis and clock doubles are
reused: the real ASGI app with a fake Redis in the lifespan's slot and an
injected clock. `issue_service.create_issue` is replaced with one that always
raises a sentinel 409, so the status code alone answers "did the limiter let it
through?" — 409 reached the handler, 429 did not — with no database, no image
pipeline and no event publishing involved.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.core.exceptions import ConflictError, RateLimitError
from app.core.security import create_access_token
from app.database import get_db
from app.dependencies import (
    ISSUE_RATE_LIMIT_ANONYMOUS,
    ISSUE_RATE_LIMIT_REGISTERED,
    ISSUE_RATE_LIMIT_WINDOW_SECONDS,
)
from app.main import create_app
from tests.unit.test_login_rate_limit import BrokenRedis, FakeClock, FakeRedis

ISSUES_URL = "/v1/issues"
FORM = {"category": "POTHOLE", "latitude": "12.9716", "longitude": "77.5946"}
REACHED_HANDLER = 409

CITIZEN_ID = uuid.UUID("00000000-0000-0000-0000-00000000c171")


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def clock() -> Iterator[FakeClock]:
    fake = FakeClock()
    with patch("app.core.rate_limiter.time.time", fake):
        yield fake


@pytest.fixture(autouse=True)
def no_captcha(monkeypatch: pytest.MonkeyPatch) -> None:
    """Anonymous submissions skip CAPTCHA when no key is configured — keep it that way here."""
    monkeypatch.setattr(settings, "RECAPTCHA_SECRET_KEY", "")


@pytest.fixture
def sentinel_handler() -> Iterator[AsyncMock]:
    failing = AsyncMock(side_effect=ConflictError(code="HANDLER_REACHED", message="reached the handler"))
    with patch("app.services.issue_service.create_issue", failing):
        yield failing


def _citizen_token(user_id: uuid.UUID = CITIZEN_ID) -> dict[str, str]:
    token = create_access_token(user_id=str(user_id), role="CITIZEN", email="citizen@example.com")
    return {"Authorization": f"Bearer {token}"}


def _build_client(redis_client: object | None, *, active_user: bool = True) -> AsyncClient:
    """The real app, `redis_client` in the lifespan's slot, and a session that knows one citizen."""
    app = create_app()
    app.state.redis = redis_client

    async def _get(model: type, key: object) -> object:  # mirrors AsyncSession.get
        return SimpleNamespace(id=uuid.UUID(str(key)), role="CITIZEN", is_active=active_user)

    db = AsyncMock()
    db.get = _get
    app.dependency_overrides[get_db] = lambda: db
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def client(redis: FakeRedis, sentinel_handler: AsyncMock) -> AsyncIterator[AsyncClient]:
    async with _build_client(redis) as ac:
        yield ac


def test_every_rate_limit_error_carries_retry_after() -> None:
    """The header rides on the exception, so login's 429 sends it too."""
    assert RateLimitError(limit=5, window="1min", retry_after_seconds=17).headers == {"Retry-After": "17"}


def test_the_budgets_are_the_trd_figures() -> None:
    """Guards the constants against a silent edit — TRD Section 6."""
    assert (ISSUE_RATE_LIMIT_REGISTERED, ISSUE_RATE_LIMIT_ANONYMOUS, ISSUE_RATE_LIMIT_WINDOW_SECONDS) == (10, 3, 3600)


# ── Anonymous: 3/hour/IP ────────────────────────────────────────────────


async def test_anonymous_caller_gets_three_an_hour(client: AsyncClient, clock: FakeClock) -> None:
    for _ in range(ISSUE_RATE_LIMIT_ANONYMOUS):
        assert (await client.post(ISSUES_URL, data=FORM)).status_code == REACHED_HANDLER

    assert (await client.post(ISSUES_URL, data=FORM)).status_code == 429


async def test_anonymous_budget_is_keyed_by_ip(client: AsyncClient, redis: FakeRedis, clock: FakeClock) -> None:
    await client.post(ISSUES_URL, data=FORM)
    assert list(redis.sets) == ["rate_limit:issues:ip:127.0.0.1"]


async def test_the_429_carries_retry_after_and_the_error_envelope(client: AsyncClient, clock: FakeClock) -> None:
    for _ in range(ISSUE_RATE_LIMIT_ANONYMOUS + 1):
        response = await client.post(ISSUES_URL, data=FORM)

    assert response.status_code == 429
    error = response.json()["error"]
    assert error["code"] == "RATE_LIMIT_EXCEEDED"
    assert error["details"]["limit"] == ISSUE_RATE_LIMIT_ANONYMOUS
    assert error["details"]["window"] == "1h"
    retry_after = int(response.headers["Retry-After"])
    assert retry_after >= 1
    assert retry_after == error["details"]["retry_after_seconds"]


async def test_a_refused_submission_never_reaches_the_handler(
    client: AsyncClient, sentinel_handler: AsyncMock, clock: FakeClock
) -> None:
    for _ in range(ISSUE_RATE_LIMIT_ANONYMOUS + 3):
        await client.post(ISSUES_URL, data=FORM)

    assert sentinel_handler.await_count == ISSUE_RATE_LIMIT_ANONYMOUS


async def test_the_window_slides(client: AsyncClient, clock: FakeClock) -> None:
    for _ in range(ISSUE_RATE_LIMIT_ANONYMOUS + 1):
        await client.post(ISSUES_URL, data=FORM)

    clock.set(1_000.0 + ISSUE_RATE_LIMIT_WINDOW_SECONDS + 1)

    assert (await client.post(ISSUES_URL, data=FORM)).status_code == REACHED_HANDLER


# ── Signed in: 10/hour/user ─────────────────────────────────────────────


async def test_signed_in_user_gets_ten_an_hour(client: AsyncClient, clock: FakeClock) -> None:
    for _ in range(ISSUE_RATE_LIMIT_REGISTERED):
        response = await client.post(ISSUES_URL, data=FORM, headers=_citizen_token())
        assert response.status_code == REACHED_HANDLER

    assert (await client.post(ISSUES_URL, data=FORM, headers=_citizen_token())).status_code == 429


async def test_signed_in_budget_is_keyed_by_user_not_ip(
    client: AsyncClient, redis: FakeRedis, clock: FakeClock
) -> None:
    """A household behind one NAT must not share ten reports an hour."""
    other = uuid.UUID("00000000-0000-0000-0000-00000000c172")
    for _ in range(ISSUE_RATE_LIMIT_REGISTERED):
        await client.post(ISSUES_URL, data=FORM, headers=_citizen_token())

    assert (await client.post(ISSUES_URL, data=FORM, headers=_citizen_token(other))).status_code == REACHED_HANDLER
    assert f"rate_limit:issues:user:{CITIZEN_ID}" in redis.sets
    assert f"rate_limit:issues:user:{other}" in redis.sets


async def test_signed_in_and_anonymous_budgets_are_separate(client: AsyncClient, clock: FakeClock) -> None:
    for _ in range(ISSUE_RATE_LIMIT_REGISTERED):
        await client.post(ISSUES_URL, data=FORM, headers=_citizen_token())

    assert (await client.post(ISSUES_URL, data=FORM)).status_code == REACHED_HANDLER


async def test_an_invalid_token_spends_the_anonymous_budget(
    client: AsyncClient, redis: FakeRedis, clock: FakeClock
) -> None:
    """Identified exactly as the handler identifies the caller: a bad token is anonymous."""
    headers = {"Authorization": "Bearer not-a-jwt"}
    for _ in range(ISSUE_RATE_LIMIT_ANONYMOUS):
        assert (await client.post(ISSUES_URL, data=FORM, headers=headers)).status_code == REACHED_HANDLER

    assert (await client.post(ISSUES_URL, data=FORM, headers=headers)).status_code == 429
    assert list(redis.sets) == ["rate_limit:issues:ip:127.0.0.1"]


async def test_a_deactivated_account_spends_the_anonymous_budget(
    redis: FakeRedis, sentinel_handler: AsyncMock, clock: FakeClock
) -> None:
    async with _build_client(redis, active_user=False) as client:
        await client.post(ISSUES_URL, data=FORM, headers=_citizen_token())

    assert list(redis.sets) == ["rate_limit:issues:ip:127.0.0.1"]


# ── Redis down: fail open ───────────────────────────────────────────────


async def test_submissions_still_work_when_redis_commands_fail(sentinel_handler: AsyncMock, clock: FakeClock) -> None:
    """A cache outage must not stop citizens reporting hazards (D-8's tradeoff, applied here)."""
    broken = BrokenRedis()
    async with _build_client(broken) as client:
        for _ in range(ISSUE_RATE_LIMIT_ANONYMOUS + 5):
            assert (await client.post(ISSUES_URL, data=FORM)).status_code == REACHED_HANDLER

    assert broken.attempts == ISSUE_RATE_LIMIT_ANONYMOUS + 5, "each request should still have tried Redis"


async def test_submissions_still_work_when_there_is_no_redis_client(sentinel_handler: AsyncMock) -> None:
    async with _build_client(None) as client:
        for _ in range(ISSUE_RATE_LIMIT_REGISTERED + 5):
            response = await client.post(ISSUES_URL, data=FORM, headers=_citizen_token())
            assert response.status_code == REACHED_HANDLER


async def test_a_redis_outage_is_logged_not_swallowed(
    sentinel_handler: AsyncMock, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("WARNING", logger="app.dependencies"):
        async with _build_client(BrokenRedis()) as client:
            await client.post(ISSUES_URL, data=FORM)

    assert any("Issue rate limit check failed" in r.message and "fail-open" in r.message for r in caplog.records)


# ── Scope of the limit ──────────────────────────────────────────────────


async def test_reads_are_not_rate_limited(redis: FakeRedis, clock: FakeClock) -> None:
    """Only submission is budgeted; the citizen map polls the reads."""
    app = create_app()
    app.state.redis = redis
    db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: db
    with patch("app.services.issue_service.find_nearby", AsyncMock(return_value=[])):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for _ in range(ISSUE_RATE_LIMIT_ANONYMOUS + 3):
                response = await client.get(f"{ISSUES_URL}/nearby", params={"lat": 12.97, "lng": 77.59})
                assert response.status_code == 200

    assert redis.sets == {}
