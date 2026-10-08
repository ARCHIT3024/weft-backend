"""Integration tests for the real auth endpoints, against a live database.

These exercise `POST /v1/auth/{register,login,refresh,logout}` and
`GET /v1/users/me` end to end: HTTP in, Postgres out. They replace the static
mock assertions that used to live in `test_mock_server.py::TestAuthMock`.

Every test runs inside the transaction opened by the `db_session` fixture and is
rolled back afterwards, so they can run repeatedly against the same database.

The Google and Apple OAuth routes were cut from the MVP and remain static
stubs; they stay covered in `test_mock_server.py`, which needs no database.

When no test database is reachable the whole module skips (see
`_require_database`) rather than erroring — `tests/conftest.py` warns and
continues in that case, leaving each DB-backed module to opt out for itself.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from collections.abc import AsyncGenerator

import pytest
from httpx import AsyncClient
from sqlalchemy import NullPool, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings
from app.models.refresh_token import RefreshToken
from app.models.user import User

PASSWORD = "correct-horse-battery"
WRONG_PASSWORD = "wrong-horse-battery"


# ── Database gate ───────────────────────────────────────────────────────


async def _database_is_reachable() -> bool:
    """Open a throwaway connection to the test database.

    Uses its own NullPool engine rather than the session-scoped engine in
    conftest: this runs in a private event loop (see `_require_database`), and
    handing pooled connections between loops corrupts the pool for the tests
    that follow.
    """
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
    """Skip this module cleanly when there is no database to test against."""
    if not asyncio.run(_database_is_reachable()):
        pytest.skip(
            f"Test database {settings.TEST_DATABASE_URL} is unreachable; "
            "start Postgres (docker compose up -d db) to run the auth integration tests.",
        )


@pytest.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """Transactional session for one test, on an engine private to this test.

    Overrides the identically named fixture in `tests/conftest.py`, and
    `conftest`'s `app`/`client` fixtures pick this up in its place.

    The reason is event loops, not transactions. pytest-asyncio gives every test
    a fresh loop (`asyncio_default_test_loop_scope=function`), while conftest's
    `test_engine` is created once for the session and pools its connections. The
    first test to borrow a connection binds it to its own loop; the next test
    gets that same connection back on a different loop and asyncpg raises
    "got Future attached to a different loop". A NullPool engine built inside
    the running test keeps every connection in the loop that opened it.

    Isolation is otherwise identical to conftest's: one transaction per test,
    rolled back at the end, so nothing these tests write survives them.
    """
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


def _unique_email() -> str:
    """A fresh address per call, so tests never collide on the unique index."""
    return f"citizen-{uuid.uuid4().hex[:12]}@example.com"


async def _register(client: AsyncClient, email: str | None = None, **overrides) -> dict:
    body = {
        "email": email or _unique_email(),
        "password": PASSWORD,
        "name": "Test Citizen",
        **overrides,
    }
    response = await client.post("/v1/auth/register", json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def _login(client: AsyncClient, email: str, password: str = PASSWORD):
    return await client.post("/v1/auth/login", json={"email": email, "password": password})


async def _register_and_login(client: AsyncClient) -> tuple[dict, dict]:
    """Register a citizen and log in. Returns (register_body, login_body)."""
    email = _unique_email()
    registered = await _register(client, email)
    response = await _login(client, email)
    assert response.status_code == 200, response.text
    return registered, response.json()


def _auth_header(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


# ── Registration ────────────────────────────────────────────────────────


class TestRegister:
    async def test_creates_citizen_account(self, client: AsyncClient) -> None:
        email = _unique_email()
        body = await _register(client, email)

        assert body["email"] == email
        assert body["name"] == "Test Citizen"
        assert body["role"] == "CITIZEN"
        assert uuid.UUID(body["user_id"])
        assert body["created_at"]

    async def test_response_never_contains_credentials(self, client: AsyncClient) -> None:
        response = await client.post(
            "/v1/auth/register",
            json={"email": _unique_email(), "password": PASSWORD, "name": "Test Citizen"},
        )

        assert response.status_code == 201
        assert PASSWORD not in response.text
        assert "password" not in response.text.lower()

    async def test_persists_bcrypt_hash_not_plaintext(self, client: AsyncClient, db_session: AsyncSession) -> None:
        body = await _register(client)

        stored = await db_session.get(User, uuid.UUID(body["user_id"]))
        assert stored is not None
        assert stored.password_hash != PASSWORD
        assert stored.password_hash.startswith("$2b$")
        assert stored.is_active is True
        assert stored.is_anonymous is False

    async def test_duplicate_email_conflicts(self, client: AsyncClient) -> None:
        email = _unique_email()
        await _register(client, email)

        response = await client.post(
            "/v1/auth/register",
            json={"email": email, "password": PASSWORD, "name": "Someone Else"},
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "EMAIL_ALREADY_REGISTERED"

    async def test_email_is_matched_case_insensitively(self, client: AsyncClient) -> None:
        """`RegisterRequest` lowercases, so Foo@X.com and foo@x.com are one account."""
        email = _unique_email()
        await _register(client, email.upper())

        response = await client.post(
            "/v1/auth/register",
            json={"email": email, "password": PASSWORD, "name": "Someone Else"},
        )

        assert response.status_code == 409

    async def test_rejects_short_password(self, client: AsyncClient) -> None:
        response = await client.post(
            "/v1/auth/register",
            json={"email": _unique_email(), "password": "short", "name": "Test Citizen"},
        )

        assert response.status_code == 422

    async def test_rejects_malformed_email(self, client: AsyncClient) -> None:
        response = await client.post(
            "/v1/auth/register",
            json={"email": "not-an-email", "password": PASSWORD, "name": "Test Citizen"},
        )

        assert response.status_code == 422


# ── Login ───────────────────────────────────────────────────────────────


class TestLogin:
    async def test_returns_token_pair_and_profile(self, client: AsyncClient) -> None:
        registered, body = await _register_and_login(client)

        assert body["token_type"] == "bearer"
        assert body["expires_in"] == settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60
        assert body["access_token"].count(".") == 2  # header.payload.signature
        assert body["refresh_token"]
        assert body["user"]["id"] == registered["user_id"]
        assert body["user"]["role"] == "CITIZEN"
        assert body["user"]["trust_score"] == 0

    async def test_response_never_contains_credentials(self, client: AsyncClient) -> None:
        email = _unique_email()
        await _register(client, email)

        response = await _login(client, email)

        assert PASSWORD not in response.text
        assert "$2b$" not in response.text
        assert "password" not in response.json()["user"]

    async def test_access_token_authorises_users_me(self, client: AsyncClient) -> None:
        registered, body = await _register_and_login(client)

        response = await client.get("/v1/users/me", headers=_auth_header(body["access_token"]))

        assert response.status_code == 200
        assert response.json()["id"] == registered["user_id"]

    async def test_stores_only_the_sha256_hash_of_the_refresh_token(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        registered, body = await _register_and_login(client)
        raw_token = body["refresh_token"]

        rows = (
            await db_session.scalars(
                select(RefreshToken).where(RefreshToken.user_id == uuid.UUID(registered["user_id"]))
            )
        ).all()

        assert len(rows) == 1
        assert rows[0].token_hash == hashlib.sha256(raw_token.encode()).hexdigest()
        assert rows[0].token_hash != raw_token
        assert rows[0].is_revoked is False

    async def test_wrong_password_and_unknown_email_are_indistinguishable(self, client: AsyncClient) -> None:
        email = _unique_email()
        await _register(client, email)

        wrong_password = await _login(client, email, WRONG_PASSWORD)
        unknown_email = await _login(client, _unique_email(), PASSWORD)

        assert wrong_password.status_code == unknown_email.status_code == 401
        assert wrong_password.json() == unknown_email.json()
        assert wrong_password.json()["error"]["message"] == "Invalid email or password"

    async def test_unknown_email_is_not_measurably_faster(self, client: AsyncClient) -> None:
        """An unknown email must still pay for a bcrypt verification.

        Without the dummy-hash comparison the unknown-email path returns in
        ~5ms against ~180ms for a real bcrypt check — a 30x gap that trivially
        enumerates accounts. The bound below is deliberately loose (half the
        wrong-password time) so it catches that class of regression without
        flaking on a loaded CI box.
        """
        email = _unique_email()
        await _register(client, email)
        await _login(client, email, WRONG_PASSWORD)  # warm up the passlib backend

        async def _fastest_of_three(make_request) -> float:
            best = float("inf")
            for _ in range(3):
                started = time.perf_counter()
                await make_request()
                best = min(best, time.perf_counter() - started)
            return best

        wrong_password_time = await _fastest_of_three(lambda: _login(client, email, WRONG_PASSWORD))
        unknown_email_time = await _fastest_of_three(lambda: _login(client, _unique_email(), PASSWORD))

        assert unknown_email_time >= wrong_password_time * 0.5, (
            f"unknown-email login took {unknown_email_time:.4f}s vs "
            f"{wrong_password_time:.4f}s for a wrong password — account existence is timeable"
        )

    async def test_deactivated_account_cannot_log_in(self, client: AsyncClient, db_session: AsyncSession) -> None:
        email = _unique_email()
        registered = await _register(client, email)

        user = await db_session.get(User, uuid.UUID(registered["user_id"]))
        user.is_active = False
        await db_session.flush()

        response = await _login(client, email)

        assert response.status_code == 401
        assert response.json()["error"]["message"] == "Invalid email or password"

    async def test_failed_login_issues_no_refresh_token(self, client: AsyncClient, db_session: AsyncSession) -> None:
        email = _unique_email()
        registered = await _register(client, email)

        await _login(client, email, WRONG_PASSWORD)

        rows = (
            await db_session.scalars(
                select(RefreshToken).where(RefreshToken.user_id == uuid.UUID(registered["user_id"]))
            )
        ).all()
        assert rows == []


# ── Refresh ─────────────────────────────────────────────────────────────


class TestRefresh:
    async def test_rotates_and_returns_both_tokens(self, client: AsyncClient) -> None:
        _, login = await _register_and_login(client)

        response = await client.post("/v1/auth/refresh", json={"refresh_token": login["refresh_token"]})

        assert response.status_code == 200
        body = response.json()
        assert body["access_token"]
        assert body["refresh_token"]
        assert body["refresh_token"] != login["refresh_token"]
        assert body["token_type"] == "bearer"
        assert body["expires_in"] == settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60

    async def test_rotated_access_token_authorises_users_me(self, client: AsyncClient) -> None:
        registered, login = await _register_and_login(client)

        rotated = (await client.post("/v1/auth/refresh", json={"refresh_token": login["refresh_token"]})).json()

        response = await client.get("/v1/users/me", headers=_auth_header(rotated["access_token"]))
        assert response.status_code == 200
        assert response.json()["id"] == registered["user_id"]

    async def test_old_token_is_revoked_in_the_same_transaction(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        registered, login = await _register_and_login(client)
        old_hash = hashlib.sha256(login["refresh_token"].encode()).hexdigest()

        await client.post("/v1/auth/refresh", json={"refresh_token": login["refresh_token"]})

        rows = (
            await db_session.scalars(
                select(RefreshToken).where(RefreshToken.user_id == uuid.UUID(registered["user_id"]))
            )
        ).all()
        by_hash = {row.token_hash: row for row in rows}

        assert len(rows) == 2, "rotation must insert the replacement, not overwrite the old row"
        assert by_hash[old_hash].is_revoked is True
        assert next(row for row in rows if not row.is_revoked).token_hash != old_hash

    async def test_reusing_a_rotated_token_is_rejected(self, client: AsyncClient) -> None:
        _, login = await _register_and_login(client)
        await client.post("/v1/auth/refresh", json={"refresh_token": login["refresh_token"]})

        response = await client.post("/v1/auth/refresh", json={"refresh_token": login["refresh_token"]})

        assert response.status_code == 401
        assert response.json()["error"]["message"] == "Invalid or expired refresh token"

    async def test_unknown_token_is_rejected(self, client: AsyncClient) -> None:
        response = await client.post("/v1/auth/refresh", json={"refresh_token": "not-a-real-token"})

        assert response.status_code == 401
        assert response.json()["error"]["message"] == "Invalid or expired refresh token"

    async def test_an_access_token_is_not_a_refresh_token(self, client: AsyncClient) -> None:
        """The two token types are not interchangeable, despite both being opaque to clients."""
        _, login = await _register_and_login(client)

        response = await client.post("/v1/auth/refresh", json={"refresh_token": login["access_token"]})

        assert response.status_code == 401

    async def test_missing_token_is_a_bad_request(self, client: AsyncClient) -> None:
        empty_body = await client.post("/v1/auth/refresh", json={})
        no_body = await client.post("/v1/auth/refresh")

        for response in (empty_body, no_body):
            assert response.status_code == 400, response.text
            assert response.json()["error"]["code"] == "MISSING_REFRESH_TOKEN"

    async def test_deactivated_user_cannot_refresh(self, client: AsyncClient, db_session: AsyncSession) -> None:
        registered, login = await _register_and_login(client)

        user = await db_session.get(User, uuid.UUID(registered["user_id"]))
        user.is_active = False
        await db_session.flush()

        response = await client.post("/v1/auth/refresh", json={"refresh_token": login["refresh_token"]})

        assert response.status_code == 401


# ── Logout ──────────────────────────────────────────────────────────────


class TestLogout:
    async def test_revokes_the_refresh_token(self, client: AsyncClient, db_session: AsyncSession) -> None:
        registered, login = await _register_and_login(client)

        response = await client.post("/v1/auth/logout", json={"refresh_token": login["refresh_token"]})

        assert response.status_code == 204
        assert response.content == b""

        rows = (
            await db_session.scalars(
                select(RefreshToken).where(RefreshToken.user_id == uuid.UUID(registered["user_id"]))
            )
        ).all()
        assert all(row.is_revoked for row in rows)

    async def test_revoked_token_can_no_longer_refresh(self, client: AsyncClient) -> None:
        _, login = await _register_and_login(client)
        await client.post("/v1/auth/logout", json={"refresh_token": login["refresh_token"]})

        response = await client.post("/v1/auth/refresh", json={"refresh_token": login["refresh_token"]})

        assert response.status_code == 401

    async def test_is_idempotent(self, client: AsyncClient) -> None:
        """Repeat, unknown and absent tokens all return 204 — never an existence oracle."""
        _, login = await _register_and_login(client)

        first = await client.post("/v1/auth/logout", json={"refresh_token": login["refresh_token"]})
        second = await client.post("/v1/auth/logout", json={"refresh_token": login["refresh_token"]})
        unknown = await client.post("/v1/auth/logout", json={"refresh_token": "never-issued"})
        empty = await client.post("/v1/auth/logout", json={})
        no_body = await client.post("/v1/auth/logout")

        for response in (first, second, unknown, empty, no_body):
            assert response.status_code == 204, response.text
            assert response.content == b""

    async def test_leaves_other_sessions_alone(self, client: AsyncClient) -> None:
        """Single-device logout: the other device's token keeps working."""
        email = _unique_email()
        await _register(client, email)
        phone_session = (await _login(client, email)).json()
        laptop_session = (await _login(client, email)).json()

        await client.post("/v1/auth/logout", json={"refresh_token": phone_session["refresh_token"]})

        response = await client.post("/v1/auth/refresh", json={"refresh_token": laptop_session["refresh_token"]})
        assert response.status_code == 200


# ── GET /users/me ───────────────────────────────────────────────────────


class TestUsersMe:
    async def test_returns_the_callers_profile(self, client: AsyncClient) -> None:
        registered, login = await _register_and_login(client)

        response = await client.get("/v1/users/me", headers=_auth_header(login["access_token"]))

        assert response.status_code == 200
        body = response.json()
        assert body == {
            "id": registered["user_id"],
            "email": registered["email"],
            "name": "Test Citizen",
            "role": "CITIZEN",
            "trust_score": 0.0,
            "total_points": 0,
            "title": None,
            "is_anonymous": False,
            "preferred_lang": "en",
            "created_at": body["created_at"],
        }

    async def test_never_exposes_credentials(self, client: AsyncClient) -> None:
        _, login = await _register_and_login(client)

        response = await client.get("/v1/users/me", headers=_auth_header(login["access_token"]))

        assert "password" not in response.text.lower()
        assert "$2b$" not in response.text
        assert login["refresh_token"] not in response.text

    async def test_requires_a_token(self, client: AsyncClient) -> None:
        response = await client.get("/v1/users/me")

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "UNAUTHORIZED"

    async def test_rejects_a_garbage_token(self, client: AsyncClient) -> None:
        response = await client.get("/v1/users/me", headers=_auth_header("not.a.jwt"))

        assert response.status_code == 401

    async def test_rejects_a_valid_token_for_a_deactivated_user(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        """A signed token is not enough — `get_current_user` re-checks is_active."""
        registered, login = await _register_and_login(client)

        assert (await client.get("/v1/users/me", headers=_auth_header(login["access_token"]))).status_code == 200

        user = await db_session.get(User, uuid.UUID(registered["user_id"]))
        user.is_active = False
        await db_session.flush()

        response = await client.get("/v1/users/me", headers=_auth_header(login["access_token"]))

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "UNAUTHORIZED"
