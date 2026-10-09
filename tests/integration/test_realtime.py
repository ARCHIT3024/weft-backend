"""Integration tests for the realtime dashboard feed, against live Postgres and Redis.

What is being proven, end to end over a real WebSocket and real pub/sub:

* **Zone scoping.** An authority in zone A hears zone A and nothing else — not
  zone B, not issues outside every zone. An admin hears everything.
* **Every documented event fires** with the documented envelope:
  `issue.created`, `issue.status_changed`, `issue.assigned`, and
  `issue.high_upvote_alert` exactly once at the threshold, even when a vote at
  the boundary is withdrawn and re-cast.
* **Publish happens only after commit.** A request whose commit fails
  publishes nothing.
* **Fail open.** No Redis, or an unreachable one, never fails a submission,
  a status change or an upvote.
* **The socket refuses** citizens, bad, expired and unknown tokens, deactivated
  accounts, and malformed or missing auth messages, each with its documented
  close code; it closes at token expiry, on deactivation and on a changed zone
  assignment; and it releases its Redis subscription on disconnect.
* **Fan-out works across API instances** — the reason for Redis at all.

The socket is driven in-process by `_Socket`, a minimal ASGI WebSocket client,
rather than Starlette's `TestClient`: `TestClient` runs the app on its own
event loop in another thread, where the test's rolled-back database
connection cannot be used.

Every test moves the feed onto a private channel prefix, so nothing here can
cross-talk with a dev server or another run sharing the same Redis.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import socket
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from jose import jwt
from redis.asyncio import ConnectionPool, Redis
from sqlalchemy import NullPool, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings
from app.core import events
from app.core.security import create_access_token
from app.database import get_db
from app.dependencies import enforce_issue_rate_limit
from app.main import REDIS_COMMAND_TIMEOUT_SECONDS, REDIS_CONNECT_TIMEOUT_SECONDS, create_app
from app.models.authority_user import AuthorityUser
from app.models.authority_zone import AuthorityZone
from app.models.department import Department
from app.models.user import User
from app.routers import websocket as ws
from app.schemas.issue import IssueSummary

# Inside zone A.
A_LAT, A_LNG = 12.9716, 77.5946
# Inside zone B, which does not overlap A.
B_LAT, B_LNG = 13.0500, 77.7000
# Outside every zone.
FAR_LAT, FAR_LNG = 1.0, 1.0

ZONE_A_EWKT = "SRID=4326;POLYGON((77.5700 12.9500, 77.6100 12.9500, 77.6100 12.9900, 77.5700 12.9900, 77.5700 12.9500))"
ZONE_B_EWKT = "SRID=4326;POLYGON((77.6800 13.0300, 77.7200 13.0300, 77.7200 13.0700, 77.6800 13.0700, 77.6800 13.0300))"

ENVELOPE_KEYS = {"type", "event_id", "issue_id", "zone_id", "department_id", "occurred_at", "data"}
WS_PATH = "/v1/ws/dashboard"


# ── Gates (mirrors test_issues.py) ──────────────────────────────────────


async def _database_is_reachable() -> bool:
    engine = create_async_engine(settings.TEST_DATABASE_URL, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:  # every failure mode here means "no database reachable"
        return False
    else:
        return True
    finally:
        await engine.dispose()


async def _redis_is_reachable() -> bool:
    client = Redis.from_url(settings.REDIS_URL, socket_connect_timeout=1.0, socket_timeout=1.0)
    try:
        await client.ping()
    except Exception:  # every failure mode here means "no Redis reachable"
        return False
    else:
        return True
    finally:
        await client.aclose(close_connection_pool=True)


@pytest.fixture(scope="module", autouse=True)
def _require_services() -> None:
    if not asyncio.run(_database_is_reachable()):
        pytest.skip(
            f"Test database {settings.TEST_DATABASE_URL} is unreachable; "
            "start Postgres (docker compose up -d db) to run the realtime integration tests.",
        )
    if not asyncio.run(_redis_is_reachable()):
        pytest.skip(
            f"Redis {settings.REDIS_URL} is unreachable; "
            "start Redis (docker compose up -d redis) to run the realtime integration tests.",
        )


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """One rolled-back transaction per test, on a loop-private engine (see test_issues.py)."""
    engine = create_async_engine(settings.TEST_DATABASE_URL, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            transaction = await conn.begin()
            session = AsyncSession(bind=conn, expire_on_commit=False)
            try:
                yield session
            finally:
                await session.close()
                await transaction.rollback()
    finally:
        await engine.dispose()


@pytest.fixture
async def redis_client(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[Redis, None]:
    """Real Redis, configured like the lifespan pool, on a private channel prefix."""
    run = secrets.token_hex(6)
    monkeypatch.setattr(events, "CHANNEL_PREFIX", f"test-realtime-{run}:ws:zone:")
    monkeypatch.setattr(events, "ALERT_MARKER_PREFIX", f"test-realtime-{run}:alert:")
    client = _lifespan_like_client(settings.REDIS_URL)
    try:
        yield client
    finally:
        markers = [key async for key in client.scan_iter(match=f"test-realtime-{run}:*")]
        if markers:
            await client.delete(*markers)
        await client.aclose(close_connection_pool=True)


def _build_app(db_session: AsyncSession, redis: Redis | None) -> FastAPI:
    test_app = create_app()

    async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    async def _no_rate_limit() -> None:
        # The submission rate limit is Redis-backed and has its own tests;
        # these submit far more than 3 anonymous reports an hour.
        return None

    test_app.dependency_overrides[get_db] = _override_get_db
    test_app.dependency_overrides[enforce_issue_rate_limit] = _no_rate_limit
    test_app.state.redis = redis
    return test_app


@pytest.fixture
async def app(db_session: AsyncSession, redis_client: Redis) -> FastAPI:
    return _build_app(db_session, redis_client)


@pytest.fixture
async def client(app: FastAPI) -> AsyncGenerator[AsyncClient, None]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


# ── In-process WebSocket client ─────────────────────────────────────────


class _Closed(Exception):  # noqa: N818 — mirrors the protocol event, not an error
    def __init__(self, code: int, reason: str | None) -> None:
        super().__init__(f"closed {code} {reason}")
        self.code = code
        self.reason = reason


class _Socket:
    """Drives one ASGI WebSocket connection on the test's own event loop."""

    def __init__(self, app: FastAPI, path: str = WS_PATH) -> None:
        self._app = app
        self._path = path
        self._to_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._from_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self.closed: _Closed | None = None

    async def __aenter__(self) -> _Socket:
        scope = {
            "type": "websocket",
            "asgi": {"version": "3.0"},
            "scheme": "ws",
            "path": self._path,
            "raw_path": self._path.encode(),
            "root_path": "",
            "query_string": b"",
            "headers": [(b"host", b"test")],
            "client": ("127.0.0.1", 50000),
            "server": ("test", 80),
            "subprotocols": [],
        }
        await self._to_app.put({"type": "websocket.connect"})
        self._task = asyncio.create_task(self._app(scope, self._to_app.get, self._from_app.put))
        accepted = await asyncio.wait_for(self._from_app.get(), 5)
        assert accepted["type"] == "websocket.accept", accepted
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.disconnect()

    @property
    def finished(self) -> bool:
        return self._task is not None and self._task.done()

    async def disconnect(self) -> None:
        assert self._task is not None
        if not self._task.done():
            await self._to_app.put({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(self._task, 10)  # also surfaces any exception the handler raised

    async def send(self, payload: dict[str, Any]) -> None:
        await self._to_app.put({"type": "websocket.receive", "text": json.dumps(payload)})

    async def send_text(self, text_frame: str) -> None:
        await self._to_app.put({"type": "websocket.receive", "text": text_frame})

    async def receive(self, timeout: float = 5.0) -> dict[str, Any]:
        """Next server message. Raises `_Closed` if the server closed instead."""
        message = await asyncio.wait_for(self._from_app.get(), timeout)
        if message["type"] == "websocket.close":
            self.closed = _Closed(message.get("code", 1000), message.get("reason"))
            raise self.closed
        return json.loads(message["text"])

    async def next_event(self, timeout: float = 5.0) -> dict[str, Any]:
        """Next domain event, skipping heartbeats."""
        while True:
            message = await self.receive(timeout)
            if message["type"] != "ping":
                return message

    async def expect_close(self, timeout: float = 5.0) -> _Closed:
        """Read until the server closes; anything else on the way must be a heartbeat."""
        while True:
            try:
                message = await self.receive(timeout)
            except _Closed as closed:
                return closed
            assert message["type"] == "ping", f"expected a close, got {message}"

    async def authenticate(self, token: str) -> dict[str, Any]:
        await self.send({"type": "auth", "token": token})
        ready = await self.receive()
        assert ready["type"] == "ready", ready
        return ready


# ── Data helpers ────────────────────────────────────────────────────────


async def _zone(db: AsyncSession, ewkt: str) -> uuid.UUID:
    zone_id = uuid.uuid4()
    await db.execute(
        text("INSERT INTO zones (id, name, boundary, is_active) VALUES (:id, :name, ST_GeomFromEWKT(:ewkt), TRUE)"),
        {"id": zone_id, "name": f"Realtime zone {zone_id.hex[:8]}", "ewkt": ewkt},
    )
    await db.flush()
    return zone_id


async def _user(db: AsyncSession, role: str = "CITIZEN", *, is_active: bool = True) -> User:
    user = User(
        email=f"{role.lower()}-{uuid.uuid4().hex[:12]}@example.com",
        name=f"Test {role.title()}",
        password_hash="x",
        role=role,
        is_anonymous=False,
        is_active=is_active,
    )
    db.add(user)
    await db.flush()
    return user


async def _department_id(db: AsyncSession) -> uuid.UUID:
    department_id = await db.scalar(select(Department.id).order_by(Department.code).limit(1))
    assert department_id is not None, "department seed (migration 014) has not run"
    return department_id


async def _authority(db: AsyncSession, *zone_ids: uuid.UUID, is_active: bool = True) -> tuple[User, AuthorityUser]:
    user = await _user(db, "AUTHORITY", is_active=is_active)
    profile = AuthorityUser(
        user_id=user.id,
        department_id=await _department_id(db),
        employee_id=f"EMP-{uuid.uuid4().hex[:10]}",
    )
    db.add(profile)
    await db.flush()
    for zone_id in zone_ids:
        db.add(AuthorityZone(authority_user_id=profile.id, zone_id=zone_id))
    await db.flush()
    return user, profile


def _token(user: User) -> str:
    return create_access_token(user_id=str(user.id), role=user.role, email=user.email)


def _token_expiring_in(user: User, seconds: float) -> str:
    now = datetime.now(UTC)
    claims = {
        "sub": str(user.id),
        "role": user.role,
        "iat": now,
        "exp": now + timedelta(seconds=seconds),
        "jti": secrets.token_hex(16),
    }
    return jwt.encode(claims, settings.JWT_PRIVATE_KEY, algorithm=settings.JWT_ALGORITHM)


def _auth(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(user)}"}


async def _submit(client: AsyncClient, lat: float, lng: float, **overrides: str) -> dict[str, Any]:
    form = {"category": "POTHOLE", "latitude": str(lat), "longitude": str(lng), "description": "Pothole", **overrides}
    response = await client.post("/v1/issues", data=form)
    assert response.status_code == 201, response.text
    return response.json()


class _Tap:
    """A plain pattern subscription on every zone channel, for publisher-side tests.

    `drain()` publishes a sentinel and reads up to it. Background tasks have
    finished by the time an ASGI request returns, and Redis delivers to one
    subscriber in order, so everything published before the sentinel is
    exactly what the requests published — an empty list proves absence
    without sleeping.
    """

    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._pubsub = redis.pubsub()

    async def __aenter__(self) -> _Tap:
        await self._pubsub.psubscribe(events.all_zones_pattern())
        confirmation = await self._pubsub.get_message(timeout=5)
        assert confirmation is not None and confirmation["type"] == "psubscribe"
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self._pubsub.aclose()

    async def drain(self) -> list[dict[str, Any]]:
        sentinel = uuid.uuid4().hex
        await self._redis.publish(f"{events.CHANNEL_PREFIX}sentinel", sentinel)
        received: list[dict[str, Any]] = []
        while True:
            message = await self._pubsub.get_message(ignore_subscribe_messages=True, timeout=5)
            assert message is not None, "sentinel never arrived"
            if message["data"] == sentinel:
                return received
            received.append(json.loads(message["data"]))


# ── Zone scoping, end to end ────────────────────────────────────────────


class TestZoneScoping:
    async def test_authority_hears_its_zone_and_not_another_or_none(
        self, app: FastAPI, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        zone_a = await _zone(db_session, ZONE_A_EWKT)
        await _zone(db_session, ZONE_B_EWKT)
        authority, _ = await _authority(db_session, zone_a)

        async with _Socket(app) as sock:
            ready = await sock.authenticate(_token(authority))
            assert ready["zone_ids"] == [str(zone_a)]
            assert ready["all_zones"] is False

            first = await _submit(client, A_LAT, A_LNG)
            await _submit(client, B_LAT, B_LNG)  # zone B: must not arrive
            await _submit(client, FAR_LAT, FAR_LNG)  # no zone: admins only
            last = await _submit(client, A_LAT, A_LNG)

            # Ordered delivery: if either of the middle two had been
            # delivered it would sit between these.
            assert (await sock.next_event())["issue_id"] == first["issue_id"]
            assert (await sock.next_event())["issue_id"] == last["issue_id"]

    async def test_admin_hears_every_zone_and_issues_with_no_zone(
        self, app: FastAPI, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        zone_a = await _zone(db_session, ZONE_A_EWKT)
        zone_b = await _zone(db_session, ZONE_B_EWKT)
        admin = await _user(db_session, "ADMIN")

        async with _Socket(app) as sock:
            ready = await sock.authenticate(_token(admin))
            assert ready["all_zones"] is True

            in_a = await _submit(client, A_LAT, A_LNG)
            in_b = await _submit(client, B_LAT, B_LNG)
            nowhere = await _submit(client, FAR_LAT, FAR_LNG)

            received = [await sock.next_event() for _ in range(3)]
            assert [e["issue_id"] for e in received] == [in_a["issue_id"], in_b["issue_id"], nowhere["issue_id"]]
            assert [e["zone_id"] for e in received] == [str(zone_a), str(zone_b), None]

    async def test_issue_created_carries_an_issue_summary_for_the_map(
        self, app: FastAPI, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        zone_a = await _zone(db_session, ZONE_A_EWKT)
        authority, _ = await _authority(db_session, zone_a)

        async with _Socket(app) as sock:
            await sock.authenticate(_token(authority))
            created = await _submit(client, A_LAT, A_LNG, description="Water main burst")
            event = await sock.next_event()

        assert set(event) == ENVELOPE_KEYS
        assert event["type"] == "issue.created"
        assert event["issue_id"] == created["issue_id"]
        assert event["zone_id"] == str(zone_a)
        assert event["department_id"] == created["department_id"]
        assert event["occurred_at"].endswith("Z")

        summary = IssueSummary.model_validate(event["data"]["issue"])
        assert set(event["data"]["issue"]) == set(IssueSummary.model_fields)
        assert str(summary.id) == created["issue_id"]
        assert summary.issue_number == created["issue_number"]
        assert summary.status == "REPORTED"
        assert (summary.latitude, summary.longitude) == (A_LAT, A_LNG)
        assert summary.description == "Water main burst"
        assert summary.upvote_count == 0

    async def test_authority_with_no_zones_connects_and_hears_nothing(
        self, app: FastAPI, client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await _zone(db_session, ZONE_A_EWKT)
        authority, _ = await _authority(db_session)
        monkeypatch.setattr(ws, "HEARTBEAT_INTERVAL_SECONDS", 0.1)

        async with _Socket(app) as sock:
            ready = await sock.authenticate(_token(authority))
            assert ready["zone_ids"] == []
            await _submit(client, A_LAT, A_LNG)
            # Only heartbeats arrive: the socket is alive, and silent.
            for _ in range(3):
                assert (await sock.receive())["type"] == "ping"

    async def test_fan_out_reaches_a_socket_on_another_api_instance(
        self, client: AsyncClient, db_session: AsyncSession, redis_client: Redis
    ) -> None:
        """Published through one app, heard on another sharing only Redis."""
        zone_a = await _zone(db_session, ZONE_A_EWKT)
        authority, _ = await _authority(db_session, zone_a)
        other_redis = _lifespan_like_client(settings.REDIS_URL)
        other_instance = _build_app(db_session, other_redis)
        try:
            async with _Socket(other_instance) as sock:
                await sock.authenticate(_token(authority))
                created = await _submit(client, A_LAT, A_LNG)
                assert (await sock.next_event())["issue_id"] == created["issue_id"]
        finally:
            await other_redis.aclose(close_connection_pool=True)


# ── Triage events ───────────────────────────────────────────────────────


class TestTriageEvents:
    async def test_status_change_is_published_with_both_statuses(
        self, app: FastAPI, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        zone_a = await _zone(db_session, ZONE_A_EWKT)
        authority, _ = await _authority(db_session, zone_a)
        created = await _submit(client, A_LAT, A_LNG)

        async with _Socket(app) as sock:
            await sock.authenticate(_token(authority))
            response = await client.patch(
                f"/v1/issues/{created['issue_id']}/status",
                json={"status": "IN_PROGRESS", "note": "Crew dispatched"},
                headers=_auth(authority),
            )
            assert response.status_code == 200, response.text
            event = await sock.next_event()

        assert set(event) == ENVELOPE_KEYS
        assert event["type"] == "issue.status_changed"
        assert event["issue_id"] == created["issue_id"]
        assert event["data"]["previous_status"] == "REPORTED"
        assert event["data"]["new_status"] == "IN_PROGRESS"
        assert event["data"]["issue"]["status"] == "IN_PROGRESS"
        assert event["data"]["changed_by_id"] == str(authority.id)
        assert event["data"]["note"] == "Crew dispatched"

    async def test_a_refused_transition_publishes_nothing(
        self, client: AsyncClient, db_session: AsyncSession, redis_client: Redis
    ) -> None:
        await _zone(db_session, ZONE_A_EWKT)
        admin = await _user(db_session, "ADMIN")
        created = await _submit(client, A_LAT, A_LNG)

        async with _Tap(redis_client) as tap:
            response = await client.patch(
                f"/v1/issues/{created['issue_id']}/status", json={"status": "REPORTED"}, headers=_auth(admin)
            )
            assert response.status_code == 400
            assert await tap.drain() == []

    async def test_assignment_is_published(self, app: FastAPI, client: AsyncClient, db_session: AsyncSession) -> None:
        zone_a = await _zone(db_session, ZONE_A_EWKT)
        authority, profile = await _authority(db_session, zone_a)
        admin = await _user(db_session, "ADMIN")
        created = await _submit(client, A_LAT, A_LNG)

        async with _Socket(app) as sock:
            await sock.authenticate(_token(authority))
            response = await client.patch(
                f"/v1/issues/{created['issue_id']}/assign",
                json={"assigned_to_id": str(profile.id)},
                headers=_auth(admin),
            )
            assert response.status_code == 200, response.text
            event = await sock.next_event()

        assert set(event) == ENVELOPE_KEYS
        assert event["type"] == "issue.assigned"
        assert event["data"]["assigned_to_id"] == str(profile.id)
        assert event["data"]["issue"]["assigned_to_id"] == str(profile.id)
        assert event["data"]["assigned_by_id"] == str(admin.id)
        assert event["data"]["assigned_at"].endswith("Z")


# ── High-upvote alert ───────────────────────────────────────────────────


async def _set_threshold(db: AsyncSession, department_id: str, threshold: int) -> None:
    await db.execute(
        text("UPDATE departments SET upvote_alert_threshold = :t WHERE id = :d"),
        {"t": threshold, "d": uuid.UUID(department_id)},
    )
    await db.flush()


class TestHighUpvoteAlert:
    async def test_fires_exactly_once_at_the_threshold(
        self, client: AsyncClient, db_session: AsyncSession, redis_client: Redis
    ) -> None:
        await _zone(db_session, ZONE_A_EWKT)
        created = await _submit(client, A_LAT, A_LNG)
        await _set_threshold(db_session, created["department_id"], 2)
        voters = [await _user(db_session) for _ in range(3)]
        url = f"/v1/issues/{created['issue_id']}/upvote"

        async with _Tap(redis_client) as tap:
            assert (await client.post(url, headers=_auth(voters[0]))).json()["upvote_count"] == 1
            assert await tap.drain() == []

            assert (await client.post(url, headers=_auth(voters[1]))).json()["upvote_count"] == 2
            [alert] = await tap.drain()

            # Withdraw and re-cast at the boundary: back to exactly 2, no repeat.
            assert (await client.delete(url, headers=_auth(voters[1]))).status_code == 204
            assert (await client.post(url, headers=_auth(voters[1]))).json()["upvote_count"] == 2
            # Past the threshold: no repeat either.
            assert (await client.post(url, headers=_auth(voters[2]))).json()["upvote_count"] == 3
            assert await tap.drain() == []

        assert set(alert) == ENVELOPE_KEYS
        assert alert["type"] == "issue.high_upvote_alert"
        assert alert["issue_id"] == created["issue_id"]
        assert alert["data"]["threshold"] == 2
        assert alert["data"]["upvote_count"] == 2
        assert alert["data"]["issue"]["upvote_count"] == 2

    async def test_the_alert_reaches_the_zone_dashboard(
        self, app: FastAPI, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        zone_a = await _zone(db_session, ZONE_A_EWKT)
        authority, _ = await _authority(db_session, zone_a)
        created = await _submit(client, A_LAT, A_LNG)
        await _set_threshold(db_session, created["department_id"], 1)
        voter = await _user(db_session)

        async with _Socket(app) as sock:
            await sock.authenticate(_token(authority))
            response = await client.post(f"/v1/issues/{created['issue_id']}/upvote", headers=_auth(voter))
            assert response.status_code == 201
            event = await sock.next_event()

        assert event["type"] == "issue.high_upvote_alert"
        assert event["zone_id"] == str(zone_a)


# ── Publish only after commit ───────────────────────────────────────────


class TestAfterCommit:
    async def test_a_failed_commit_publishes_nothing(
        self, app: FastAPI, db_session: AsyncSession, redis_client: Redis
    ) -> None:
        """An event for a change that rolled back would be a lie."""
        await _zone(db_session, ZONE_A_EWKT)

        async def _commit_fails() -> AsyncGenerator[AsyncSession, None]:
            yield db_session
            raise RuntimeError("simulated commit failure")

        app.dependency_overrides[get_db] = _commit_fails
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with _Tap(redis_client) as tap, AsyncClient(transport=transport, base_url="http://test") as failing:
            response = await failing.post(
                "/v1/issues", data={"category": "POTHOLE", "latitude": str(A_LAT), "longitude": str(A_LNG)}
            )
            assert response.status_code == 500
            assert await tap.drain() == []


# ── Fail open ───────────────────────────────────────────────────────────


def _unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _lifespan_like_client(url: str) -> Redis:
    """A client built exactly as `app.main.lifespan` builds the shared one.

    That matters for the outage tests: a pool-built connection does not retry,
    so a dead Redis costs one bounded timeout. `Redis(host=...)` would instead
    retry ten times and measure a configuration production does not run.
    """
    pool = ConnectionPool.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=REDIS_CONNECT_TIMEOUT_SECONDS,
        socket_timeout=REDIS_COMMAND_TIMEOUT_SECONDS,
    )
    return Redis(connection_pool=pool)


def _unreachable_redis() -> Redis:
    return _lifespan_like_client(f"redis://127.0.0.1:{_unused_port()}/0")


class TestFailOpen:
    @pytest.mark.parametrize("redis_state", ["absent", "unreachable"])
    async def test_writes_succeed_without_redis(self, db_session: AsyncSession, redis_state: str) -> None:
        redis = _unreachable_redis() if redis_state == "unreachable" else None
        app = _build_app(db_session, redis)
        await _zone(db_session, ZONE_A_EWKT)
        admin = await _user(db_session, "ADMIN")
        voter = await _user(db_session)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                created = await _submit(client, A_LAT, A_LNG)
                await _set_threshold(db_session, created["department_id"], 1)  # the upvote will cross it

                upvote = await client.post(f"/v1/issues/{created['issue_id']}/upvote", headers=_auth(voter))
                assert upvote.status_code == 201
                status_change = await client.patch(
                    f"/v1/issues/{created['issue_id']}/status", json={"status": "IN_PROGRESS"}, headers=_auth(admin)
                )
                assert status_change.status_code == 200
        finally:
            if redis is not None:
                await redis.aclose(close_connection_pool=True)


# ── Socket authentication ───────────────────────────────────────────────


class TestSocketAuth:
    async def test_citizen_is_refused_as_forbidden(self, app: FastAPI, db_session: AsyncSession) -> None:
        citizen = await _user(db_session)
        async with _Socket(app) as sock:
            await sock.send({"type": "auth", "token": _token(citizen)})
            closed = await sock.expect_close()
        assert closed.code == 4403

    async def test_a_garbage_token_is_unauthorized(self, app: FastAPI) -> None:
        async with _Socket(app) as sock:
            await sock.send({"type": "auth", "token": "not-a-jwt"})
            assert (await sock.expect_close()).code == 4401

    async def test_an_expired_token_is_unauthorized(self, app: FastAPI, db_session: AsyncSession) -> None:
        admin = await _user(db_session, "ADMIN")
        async with _Socket(app) as sock:
            await sock.send({"type": "auth", "token": _token_expiring_in(admin, -60)})
            assert (await sock.expect_close()).code == 4401

    async def test_a_deactivated_account_is_unauthorized(self, app: FastAPI, db_session: AsyncSession) -> None:
        """The same `is_active` re-check `get_current_user` makes."""
        zone_a = await _zone(db_session, ZONE_A_EWKT)
        authority, _ = await _authority(db_session, zone_a, is_active=False)
        async with _Socket(app) as sock:
            await sock.send({"type": "auth", "token": _token(authority)})
            assert (await sock.expect_close()).code == 4401

    async def test_a_token_for_an_unknown_user_is_unauthorized(self, app: FastAPI) -> None:
        token = create_access_token(user_id=str(uuid.uuid4()), role="ADMIN")
        async with _Socket(app) as sock:
            await sock.send({"type": "auth", "token": token})
            assert (await sock.expect_close()).code == 4401

    @pytest.mark.parametrize(
        "frame",
        ["not json", '["auth"]', '{"type": "hello"}', '{"type": "auth"}', '{"type": "auth", "token": 42}'],
    )
    async def test_a_malformed_first_message_is_a_protocol_error(self, app: FastAPI, frame: str) -> None:
        async with _Socket(app) as sock:
            await sock.send_text(frame)
            assert (await sock.expect_close()).code == 4400

    async def test_no_auth_message_in_time_closes_the_socket(
        self, app: FastAPI, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ws, "AUTH_TIMEOUT_SECONDS", 0.2)
        async with _Socket(app) as sock:
            closed = await sock.expect_close()
        assert (closed.code, closed.reason) == (4408, "auth_timeout")

    async def test_nothing_is_sent_before_authentication(self, app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ws, "AUTH_TIMEOUT_SECONDS", 0.3)
        monkeypatch.setattr(ws, "HEARTBEAT_INTERVAL_SECONDS", 0.05)
        async with _Socket(app) as sock:
            with pytest.raises(_Closed):
                await sock.receive()  # the first thing out is the close, not a heartbeat

    async def test_client_leaving_before_auth_is_clean(self, app: FastAPI) -> None:
        sock = _Socket(app)
        await sock.__aenter__()
        await sock.disconnect()
        assert sock.finished


# ── Socket lifetime ─────────────────────────────────────────────────────


class TestSocketLifetime:
    async def test_ready_describes_the_session(self, app: FastAPI, db_session: AsyncSession) -> None:
        admin = await _user(db_session, "ADMIN")
        async with _Socket(app) as sock:
            ready = await sock.authenticate(_token(admin))
        assert ready["user_id"] == str(admin.id)
        assert ready["role"] == "ADMIN"
        assert ready["expires_at"].endswith("Z")
        assert ready["heartbeat_interval_seconds"] == ws.HEARTBEAT_INTERVAL_SECONDS

    async def test_heartbeat_pings_an_idle_socket(
        self, app: FastAPI, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ws, "HEARTBEAT_INTERVAL_SECONDS", 0.1)
        admin = await _user(db_session, "ADMIN")
        async with _Socket(app) as sock:
            await sock.authenticate(_token(admin))
            ping = await sock.receive()
        assert ping["type"] == "ping"
        assert ping["sent_at"].endswith("Z")

    async def test_client_ping_gets_a_pong_and_unknown_messages_are_ignored(
        self, app: FastAPI, db_session: AsyncSession
    ) -> None:
        admin = await _user(db_session, "ADMIN")
        async with _Socket(app) as sock:
            await sock.authenticate(_token(admin))
            await sock.send({"type": "something-newer"})
            await sock.send_text("not json")
            await sock.send({"type": "ping"})
            assert await sock.next_event() == {"type": "pong"}

    async def test_socket_closes_when_the_token_expires(self, app: FastAPI, db_session: AsyncSession) -> None:
        admin = await _user(db_session, "ADMIN")
        async with _Socket(app) as sock:
            await sock.authenticate(_token_expiring_in(admin, 2))
            closed = await sock.expect_close(timeout=5)
        assert (closed.code, closed.reason) == (4401, "token_expired")

    async def test_deactivation_closes_an_open_socket(
        self, app: FastAPI, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        zone_a = await _zone(db_session, ZONE_A_EWKT)
        authority, _ = await _authority(db_session, zone_a)
        async with _Socket(app) as sock:
            await sock.authenticate(_token(authority))
            authority.is_active = False
            await db_session.flush()
            monkeypatch.setattr(ws, "REVALIDATE_INTERVAL_SECONDS", 0)
            closed = await sock.expect_close()
        assert (closed.code, closed.reason) == (4401, "account_deactivated")

    async def test_a_changed_zone_assignment_asks_the_client_to_reconnect(
        self, app: FastAPI, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        zone_a = await _zone(db_session, ZONE_A_EWKT)
        zone_b = await _zone(db_session, ZONE_B_EWKT)
        authority, profile = await _authority(db_session, zone_a)
        async with _Socket(app) as sock:
            await sock.authenticate(_token(authority))
            db_session.add(AuthorityZone(authority_user_id=profile.id, zone_id=zone_b))
            await db_session.flush()
            monkeypatch.setattr(ws, "REVALIDATE_INTERVAL_SECONDS", 0)
            closed = await sock.expect_close()
        assert (closed.code, closed.reason) == (4409, "subscription_changed")

    async def test_unchanged_revalidation_keeps_the_socket_open(
        self, app: FastAPI, client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        zone_a = await _zone(db_session, ZONE_A_EWKT)
        authority, _ = await _authority(db_session, zone_a)
        async with _Socket(app) as sock:
            await sock.authenticate(_token(authority))
            monkeypatch.setattr(ws, "REVALIDATE_INTERVAL_SECONDS", 0)
            await asyncio.sleep(1.5)  # at least one revalidation pass
            monkeypatch.setattr(ws, "REVALIDATE_INTERVAL_SECONDS", 60)
            assert not sock.finished
            await asyncio.sleep(1.1)  # let any in-flight revalidation finish before the test DB is reused
            created = await _submit(client, A_LAT, A_LNG)
            assert (await sock.next_event())["issue_id"] == created["issue_id"]

    async def test_disconnect_releases_the_redis_subscription(
        self, app: FastAPI, db_session: AsyncSession, redis_client: Redis
    ) -> None:
        zone_a = await _zone(db_session, ZONE_A_EWKT)
        authority, _ = await _authority(db_session, zone_a)
        channel = events.zone_channel(zone_a)

        sock = _Socket(app)
        await sock.__aenter__()
        await sock.authenticate(_token(authority))
        assert await redis_client.pubsub_numsub(channel) == [(channel, 1)]

        await sock.disconnect()
        assert sock.finished
        for _ in range(50):  # the server drops the subscription as it notices the closed connection
            if await redis_client.pubsub_numsub(channel) == [(channel, 0)]:
                break
            await asyncio.sleep(0.05)
        assert await redis_client.pubsub_numsub(channel) == [(channel, 0)]

    async def test_server_close_also_releases_the_subscription(
        self, app: FastAPI, db_session: AsyncSession, redis_client: Redis
    ) -> None:
        zone_a = await _zone(db_session, ZONE_A_EWKT)
        authority, _ = await _authority(db_session, zone_a)
        channel = events.zone_channel(zone_a)

        async with _Socket(app) as sock:
            await sock.authenticate(_token_expiring_in(authority, 1))
            await sock.expect_close()
        for _ in range(50):
            if await redis_client.pubsub_numsub(channel) == [(channel, 0)]:
                break
            await asyncio.sleep(0.05)
        assert await redis_client.pubsub_numsub(channel) == [(channel, 0)]


# ── Redis unavailable at connect ────────────────────────────────────────


class TestRedisUnavailableAtConnect:
    async def test_no_redis_client_closes_with_try_again_later(self, db_session: AsyncSession) -> None:
        app = _build_app(db_session, None)
        admin = await _user(db_session, "ADMIN")
        async with _Socket(app) as sock:
            await sock.send({"type": "auth", "token": _token(admin)})
            closed = await sock.expect_close()
        assert (closed.code, closed.reason) == (1013, "realtime_unavailable")

    async def test_unreachable_redis_closes_rather_than_hangs(self, db_session: AsyncSession) -> None:
        redis = _unreachable_redis()
        app = _build_app(db_session, redis)
        admin = await _user(db_session, "ADMIN")
        try:
            async with _Socket(app) as sock:
                await sock.send({"type": "auth", "token": _token(admin)})
                closed = await sock.expect_close(timeout=15)
            assert closed.code == 1013
        finally:
            await redis.aclose(close_connection_pool=True)

    async def test_auth_is_checked_before_redis(self, db_session: AsyncSession) -> None:
        """An unauthenticated client learns nothing about the server's Redis."""
        app = _build_app(db_session, None)
        async with _Socket(app) as sock:
            await sock.send({"type": "auth", "token": "not-a-jwt"})
            assert (await sock.expect_close()).code == 4401
