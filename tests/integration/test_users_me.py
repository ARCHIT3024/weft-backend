"""Integration tests for `PATCH /users/me` and `GET /users/me/reports`.

These replace the Phase 0 mock behaviour of both routes. What needs a real
database here:

* **Device registration is an upsert on `UNIQUE (device_token)`.** The same
  token registered by a second account *moves* to it — a phone has one current
  user, and the previous one's notifications must stop arriving on it.
* **The per-user device cap evicts the least recently seen token.**
* **"My reports" is the caller's own issues only**, newest first, and
  anonymous reports are in nobody's list.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator

import pytest
from httpx import AsyncClient
from sqlalchemy import NullPool, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings
from app.core.security import create_access_token
from app.models.fcm_token import FcmToken
from app.models.user import User
from app.services.user_service import MAX_DEVICE_TOKENS_PER_USER

LAT, LNG = 12.9716, 77.5946


# ── Database gate (mirrors test_issues.py) ──────────────────────────────


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
            "start Postgres (docker compose up -d db) to run the /users/me integration tests.",
        )


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


# ── Helpers ─────────────────────────────────────────────────────────────


async def _user(db: AsyncSession, role: str = "CITIZEN") -> User:
    user = User(
        email=f"{role.lower()}-{uuid.uuid4().hex[:12]}@example.com",
        name=f"Test {role.title()}",
        password_hash="x",
        role=role,
        is_anonymous=False,
        is_active=True,
    )
    db.add(user)
    await db.flush()
    return user


def _auth(user: User) -> dict[str, str]:
    token = create_access_token(user_id=str(user.id), role=user.role, email=user.email)
    return {"Authorization": f"Bearer {token}"}


async def _owner_of(db: AsyncSession, token: str) -> uuid.UUID | None:
    stmt = select(FcmToken.user_id).where(FcmToken.device_token == token).execution_options(populate_existing=True)
    return await db.scalar(stmt)


async def _token_count(db: AsyncSession, user: User) -> int:
    return await db.scalar(select(func.count()).select_from(FcmToken).where(FcmToken.user_id == user.id)) or 0


async def _submit(client: AsyncClient, user: User | None, **overrides) -> dict:
    form = {"category": "POTHOLE", "latitude": str(LAT), "longitude": str(LNG), **overrides}
    response = await client.post("/v1/issues", data=form, headers=_auth(user) if user else {})
    assert response.status_code == 201, response.text
    return response.json()


# ── PATCH /users/me — profile fields ────────────────────────────────────


class TestUpdateProfile:
    async def test_requires_authentication(self, client: AsyncClient) -> None:
        assert (await client.patch("/v1/users/me", json={"name": "x"})).status_code == 401

    async def test_updates_name_and_language_and_returns_the_profile(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        user = await _user(db_session)

        response = await client.patch(
            "/v1/users/me", json={"name": "  Asha Rao  ", "preferred_lang": "hi"}, headers=_auth(user)
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["id"] == str(user.id)
        assert body["name"] == "Asha Rao", "surrounding whitespace is stripped"
        assert body["preferred_lang"] == "hi"
        assert body["email"] == user.email

        await db_session.refresh(user)
        assert user.name == "Asha Rao"
        assert user.preferred_lang == "hi"

    async def test_omitted_and_null_fields_are_left_unchanged(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        user = await _user(db_session)
        original_name = user.name

        response = await client.patch("/v1/users/me", json={"name": None, "preferred_lang": "ta"}, headers=_auth(user))

        assert response.status_code == 200
        assert response.json()["name"] == original_name
        assert response.json()["preferred_lang"] == "ta"

    async def test_empty_body_is_a_no_op(self, client: AsyncClient, db_session: AsyncSession) -> None:
        user = await _user(db_session)
        response = await client.patch("/v1/users/me", json={}, headers=_auth(user))
        assert response.status_code == 200
        assert response.json()["name"] == user.name

    @pytest.mark.parametrize(
        "body",
        [
            {"name": ""},
            {"name": "   "},
            {"name": "x" * 101},
            {"preferred_lang": "fr"},
            {"fcm_token": ""},
            {"fcm_token": "x" * 4097},
        ],
    )
    async def test_invalid_values_are_422(self, client: AsyncClient, db_session: AsyncSession, body: dict) -> None:
        user = await _user(db_session)
        assert (await client.patch("/v1/users/me", json=body, headers=_auth(user))).status_code == 422

    @pytest.mark.parametrize("field", ["device_token", "email", "phone", "role", "trust_score"])
    async def test_unknown_or_protected_fields_are_rejected(
        self, client: AsyncClient, db_session: AsyncSession, field: str
    ) -> None:
        """`device_token` in particular: the field is `fcm_token`, and silently
        ignoring the wrong name would leave a device that never gets a push."""
        user = await _user(db_session)
        response = await client.patch("/v1/users/me", json={field: "ADMIN"}, headers=_auth(user))
        assert response.status_code == 422
        await db_session.refresh(user)
        assert user.role == "CITIZEN"

    async def test_response_never_echoes_the_device_token(self, client: AsyncClient, db_session: AsyncSession) -> None:
        user = await _user(db_session)
        response = await client.patch("/v1/users/me", json={"fcm_token": "secret-device-token"}, headers=_auth(user))
        assert response.status_code == 200
        assert "secret-device-token" not in response.text


# ── PATCH /users/me — device registration ───────────────────────────────


class TestDeviceRegistration:
    async def test_registers_a_device(self, client: AsyncClient, db_session: AsyncSession) -> None:
        user = await _user(db_session)
        token = f"fcm-{uuid.uuid4().hex}"

        response = await client.patch("/v1/users/me", json={"fcm_token": token}, headers=_auth(user))

        assert response.status_code == 200, response.text
        assert await _owner_of(db_session, token) == user.id

    async def test_reregistering_is_idempotent_and_refreshes_last_seen(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        """The app sends the token on every launch; that must not pile up rows."""
        user = await _user(db_session)
        token = f"fcm-{uuid.uuid4().hex}"

        await client.patch("/v1/users/me", json={"fcm_token": token}, headers=_auth(user))
        first_seen = await db_session.scalar(select(FcmToken.last_seen).where(FcmToken.device_token == token))
        await client.patch("/v1/users/me", json={"fcm_token": token}, headers=_auth(user))
        second_seen = await db_session.scalar(
            select(FcmToken.last_seen).where(FcmToken.device_token == token).execution_options(populate_existing=True)
        )

        assert await _token_count(db_session, user) == 1
        assert second_seen > first_seen

    async def test_same_token_moves_to_the_new_user(self, client: AsyncClient, db_session: AsyncSession) -> None:
        """A phone changing hands: the old owner must stop receiving on it."""
        first, second = await _user(db_session), await _user(db_session)
        token = f"fcm-{uuid.uuid4().hex}"

        await client.patch("/v1/users/me", json={"fcm_token": token}, headers=_auth(first))
        await client.patch("/v1/users/me", json={"fcm_token": token}, headers=_auth(second))

        assert await _owner_of(db_session, token) == second.id
        assert await _token_count(db_session, first) == 0

    async def test_a_user_may_have_several_devices(self, client: AsyncClient, db_session: AsyncSession) -> None:
        user = await _user(db_session)
        for _ in range(3):
            await client.patch("/v1/users/me", json={"fcm_token": f"fcm-{uuid.uuid4().hex}"}, headers=_auth(user))
        assert await _token_count(db_session, user) == 3

    async def test_device_cap_evicts_the_least_recently_seen(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        user = await _user(db_session)
        tokens = [f"fcm-{i}-{uuid.uuid4().hex}" for i in range(MAX_DEVICE_TOKENS_PER_USER + 1)]
        for token in tokens:
            await client.patch("/v1/users/me", json={"fcm_token": token}, headers=_auth(user))

        assert await _token_count(db_session, user) == MAX_DEVICE_TOKENS_PER_USER
        assert await _owner_of(db_session, tokens[0]) is None, "the oldest device is evicted"
        assert await _owner_of(db_session, tokens[-1]) == user.id

    async def test_token_and_profile_change_together(self, client: AsyncClient, db_session: AsyncSession) -> None:
        user = await _user(db_session)
        token = f"fcm-{uuid.uuid4().hex}"

        response = await client.patch(
            "/v1/users/me", json={"fcm_token": token, "preferred_lang": "bn"}, headers=_auth(user)
        )

        assert response.json()["preferred_lang"] == "bn"
        assert await _owner_of(db_session, token) == user.id


# ── GET /users/me/reports ───────────────────────────────────────────────


class TestMyReports:
    async def test_requires_authentication(self, client: AsyncClient) -> None:
        assert (await client.get("/v1/users/me/reports")).status_code == 401

    async def test_returns_only_the_callers_issues_newest_first(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        me, other = await _user(db_session), await _user(db_session)
        older = await _submit(client, me)
        newer = await _submit(client, me, category="WATER_LOGGING")
        await _submit(client, other)
        await _submit(client, None)
        # `issues.created_at` defaults to NOW(), the *transaction* timestamp, and
        # this whole test is one transaction — both issues would tie, and the
        # id tie-break is random. Production requests each get their own
        # transaction, so backdating stands in for "submitted earlier".
        await db_session.execute(
            text("UPDATE issues SET created_at = created_at - INTERVAL '1 hour' WHERE id = :id"),
            {"id": uuid.UUID(older["issue_id"])},
        )

        response = await client.get("/v1/users/me/reports", headers=_auth(me))

        assert response.status_code == 200, response.text
        body = response.json()
        assert [i["id"] for i in body["items"]] == [newer["issue_id"], older["issue_id"]]
        assert body["total"] == 2
        assert body["page"] == 1
        assert body["total_pages"] == 1

    async def test_items_have_the_issue_summary_shape(self, client: AsyncClient, db_session: AsyncSession) -> None:
        me = await _user(db_session)
        await _submit(client, me, description="Deep pothole", address_text="MG Road")

        item = (await client.get("/v1/users/me/reports", headers=_auth(me))).json()["items"][0]

        assert set(item) == {
            "id",
            "issue_number",
            "category",
            "description",
            "status",
            "latitude",
            "longitude",
            "address_text",
            "upvote_count",
            "zone_id",
            "department_id",
            "assigned_to_id",
            "resolved_at",
            "created_at",
            "updated_at",
        }
        assert item["description"] == "Deep pothole"
        assert item["latitude"] == pytest.approx(LAT)
        assert "reporter_id" not in item

    async def test_status_filter(self, client: AsyncClient, db_session: AsyncSession) -> None:
        me = await _user(db_session)
        authority = await _user(db_session, role="AUTHORITY")
        open_issue = await _submit(client, me)
        done = await _submit(client, me)
        await client.patch(
            f"/v1/issues/{done['issue_id']}/status", json={"status": "RESOLVED"}, headers=_auth(authority)
        )

        resolved = (await client.get("/v1/users/me/reports", params={"status": "RESOLVED"}, headers=_auth(me))).json()
        both = (
            await client.get(
                "/v1/users/me/reports", params=[("status", "RESOLVED"), ("status", "REPORTED")], headers=_auth(me)
            )
        ).json()

        assert [i["id"] for i in resolved["items"]] == [done["issue_id"]]
        assert {i["id"] for i in both["items"]} == {done["issue_id"], open_issue["issue_id"]}

    async def test_pagination(self, client: AsyncClient, db_session: AsyncSession) -> None:
        me = await _user(db_session)
        for _ in range(3):
            await _submit(client, me)

        body = (await client.get("/v1/users/me/reports", params={"page_size": 2, "page": 2}, headers=_auth(me))).json()

        assert body["total"] == 3
        assert body["total_pages"] == 2
        assert len(body["items"]) == 1

    @pytest.mark.parametrize("params", [{"status": "NOPE"}, {"page_size": 101}, {"page": 0}])
    async def test_invalid_params_are_422(self, client: AsyncClient, db_session: AsyncSession, params: dict) -> None:
        me = await _user(db_session)
        assert (await client.get("/v1/users/me/reports", params=params, headers=_auth(me))).status_code == 422

    async def test_authority_sees_own_reports_too(self, client: AsyncClient, db_session: AsyncSession) -> None:
        authority = await _user(db_session, role="AUTHORITY")
        issue = await _submit(client, authority)
        body = (await client.get("/v1/users/me/reports", headers=_auth(authority))).json()
        assert [i["id"] for i in body["items"]] == [issue["issue_id"]]
