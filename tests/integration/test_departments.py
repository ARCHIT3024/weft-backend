"""`GET /v1/departments` — the staff read of department SLAs, against a live database.

The dashboard draws SLA countdowns and breach badges, and until this endpoint
existed it could only hard-code 72 hours: the real per-department `sla_hours`
lived behind the ADMIN-only `/admin/departments`. Proven here: any staff role
can read it, nobody else can, it carries exactly the documented fields, and it
lists the same departments in the same order as the admin endpoint.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator

import pytest
from httpx import AsyncClient
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings
from app.core.security import create_access_token
from app.models.department import Department
from app.models.user import User

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
            "start Postgres (docker compose up -d db) to run the department integration tests.",
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


async def _user(db: AsyncSession, role: str) -> User:
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


class TestStaffDepartmentRead:
    @pytest.mark.parametrize("role", ["AUTHORITY", "ADMIN"])
    async def test_staff_read_the_real_sla(self, client: AsyncClient, db_session: AsyncSession, role: str) -> None:
        """An AUTHORITY needs no profile or zone for this: departments are city-wide config."""
        roads = Department(name="Roads Test", code=f"R{uuid.uuid4().hex[:6]}", sla_hours=48, upvote_alert_threshold=7)
        db_session.add(roads)
        await db_session.flush()

        response = await client.get("/v1/departments", headers=_auth(await _user(db_session, role)))
        assert response.status_code == 200, response.text

        item = next(i for i in response.json()["items"] if i["id"] == str(roads.id))
        assert item == {
            "id": str(roads.id),
            "name": "Roads Test",
            "code": roads.code,
            "sla_hours": 48,
            "upvote_alert_threshold": 7,
            "is_active": True,
        }

    async def test_includes_inactive_departments(self, client: AsyncClient, db_session: AsyncSession) -> None:
        """Old issues still carry a retired department's id, and still need its SLA."""
        retired = Department(name="Retired", code=f"X{uuid.uuid4().hex[:6]}", is_active=False)
        db_session.add(retired)
        await db_session.flush()

        items = (await client.get("/v1/departments", headers=_auth(await _user(db_session, "AUTHORITY")))).json()
        assert next(i for i in items["items"] if i["id"] == str(retired.id))["is_active"] is False

    async def test_matches_the_admin_list_in_content_and_order(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        admin = await _user(db_session, "ADMIN")
        staff = (await client.get("/v1/departments", headers=_auth(admin))).json()["items"]
        full = (await client.get("/v1/admin/departments", headers=_auth(admin))).json()["items"]

        assert [d["id"] for d in staff] == [d["id"] for d in full]
        assert [d["code"] for d in staff] == sorted(d["code"] for d in staff)
        for short, long in zip(staff, full, strict=True):
            assert short["sla_hours"] == long["sla_hours"]

    async def test_seeded_departments_are_listed(self, client: AsyncClient, db_session: AsyncSession) -> None:
        items = (await client.get("/v1/departments", headers=_auth(await _user(db_session, "AUTHORITY")))).json()
        assert {"PWD", "SAN"} <= {d["code"] for d in items["items"]}

    async def test_citizens_are_refused(self, client: AsyncClient, db_session: AsyncSession) -> None:
        response = await client.get("/v1/departments", headers=_auth(await _user(db_session, "CITIZEN")))
        assert response.status_code == 403

    async def test_anonymous_callers_are_refused(self, client: AsyncClient) -> None:
        assert (await client.get("/v1/departments")).status_code == 401
