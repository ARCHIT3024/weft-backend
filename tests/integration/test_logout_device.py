"""`POST /v1/auth/logout` with `fcm_token` — logging out unregisters the device.

Before this, a logged-out phone kept receiving the previous user's pushes: its
FCM registration stayed attached to them until another account claimed the
token. Logout is unauthenticated by design (D-3), so the refresh token being
revoked is the only proof of who is calling. Proven here:

* the named device token is deleted when it belongs to the refresh token's
  owner, and nothing else of theirs is touched;
* it is **ignored** — never deleted — when the refresh token is unknown,
  already revoked or absent, or when the FCM token belongs to someone else;
* the response is the same bodiless 204 in every case.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator

import pytest
from httpx import AsyncClient
from sqlalchemy import NullPool, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings
from app.models.fcm_token import FcmToken

PASSWORD = "correct-horse-battery"


# ── Database gate (mirrors test_auth.py) ────────────────────────────────


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


@pytest.fixture(scope="module", autouse=True)
def _require_database() -> None:
    if not asyncio.run(_database_is_reachable()):
        pytest.skip(
            f"Test database {settings.TEST_DATABASE_URL} is unreachable; "
            "start Postgres (docker compose up -d db) to run the logout integration tests.",
        )


@pytest.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """One rolled-back transaction per test, on a loop-private engine (see test_auth.py)."""
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


# ── Helpers ─────────────────────────────────────────────────────────────


async def _session(client: AsyncClient) -> tuple[uuid.UUID, str]:
    """Register and log in a citizen. Returns (user_id, refresh_token)."""
    email = f"citizen-{uuid.uuid4().hex[:12]}@example.com"
    registered = await client.post("/v1/auth/register", json={"email": email, "password": PASSWORD, "name": "T"})
    assert registered.status_code == 201, registered.text
    login = await client.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    return uuid.UUID(registered.json()["user_id"]), login.json()["refresh_token"]


async def _device(db: AsyncSession, user_id: uuid.UUID) -> str:
    value = f"fcm-{uuid.uuid4().hex}"
    db.add(FcmToken(user_id=user_id, device_token=value))
    await db.flush()
    return value


async def _registered(db: AsyncSession, device_token: str) -> bool:
    return await db.scalar(select(FcmToken.id).where(FcmToken.device_token == device_token)) is not None


async def _logout(client: AsyncClient, **body: str) -> None:
    response = await client.post("/v1/auth/logout", json=body)
    assert response.status_code == 204, response.text
    assert response.content == b""


# ── Tests ───────────────────────────────────────────────────────────────


class TestLogoutUnregistersTheDevice:
    async def test_deletes_the_owners_device_token(self, client: AsyncClient, db_session: AsyncSession) -> None:
        user_id, refresh = await _session(client)
        phone = await _device(db_session, user_id)

        await _logout(client, refresh_token=refresh, fcm_token=phone)

        assert not await _registered(db_session, phone)

    async def test_leaves_the_owners_other_devices_alone(self, client: AsyncClient, db_session: AsyncSession) -> None:
        """Single-device logout (D-3) applies to push too."""
        user_id, refresh = await _session(client)
        phone, tablet = await _device(db_session, user_id), await _device(db_session, user_id)

        await _logout(client, refresh_token=refresh, fcm_token=phone)

        assert not await _registered(db_session, phone)
        assert await _registered(db_session, tablet)

    async def test_cannot_unregister_someone_elses_device(self, client: AsyncClient, db_session: AsyncSession) -> None:
        """A live refresh token proves who *you* are, not who owns the FCM token you name."""
        _, my_refresh = await _session(client)
        victim_id, _ = await _session(client)
        victims_phone = await _device(db_session, victim_id)

        await _logout(client, refresh_token=my_refresh, fcm_token=victims_phone)

        assert await _registered(db_session, victims_phone)

    async def test_ignored_with_an_unknown_refresh_token(self, client: AsyncClient, db_session: AsyncSession) -> None:
        victim_id, _ = await _session(client)
        victims_phone = await _device(db_session, victim_id)

        await _logout(client, refresh_token="never-issued", fcm_token=victims_phone)

        assert await _registered(db_session, victims_phone)

    async def test_ignored_with_an_already_revoked_refresh_token(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        """A dead session proves nothing any more — not even about its own owner."""
        user_id, refresh = await _session(client)
        phone = await _device(db_session, user_id)
        await _logout(client, refresh_token=refresh)

        await _logout(client, refresh_token=refresh, fcm_token=phone)

        assert await _registered(db_session, phone)

    async def test_ignored_without_a_refresh_token(self, client: AsyncClient, db_session: AsyncSession) -> None:
        user_id, _ = await _session(client)
        phone = await _device(db_session, user_id)

        await _logout(client, fcm_token=phone)

        assert await _registered(db_session, phone)

    async def test_an_unknown_fcm_token_is_harmless(self, client: AsyncClient, db_session: AsyncSession) -> None:
        user_id, refresh = await _session(client)
        phone = await _device(db_session, user_id)

        await _logout(client, refresh_token=refresh, fcm_token="not-a-registered-token")

        assert await _registered(db_session, phone)
        second = await client.post("/v1/auth/refresh", json={"refresh_token": refresh})
        assert second.status_code == 401, "the session itself is still revoked"

    async def test_still_idempotent(self, client: AsyncClient, db_session: AsyncSession) -> None:
        user_id, refresh = await _session(client)
        phone = await _device(db_session, user_id)

        await _logout(client, refresh_token=refresh, fcm_token=phone)
        await _logout(client, refresh_token=refresh, fcm_token=phone)

        assert not await _registered(db_session, phone)
