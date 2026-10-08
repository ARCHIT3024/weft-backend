"""Integration tests for the admin endpoints, against a live database.

These replace `test_mock_server.py::TestAdminMock`, which pinned static JSON
from the Phase 0 stubs.

What is being proven here — each is a property that lives in PostgreSQL or in
the request pipeline, not in a function that could be unit-tested alone:

* **Only ADMIN gets in.** No token is a 401; CITIZEN and AUTHORITY are 403.
* **Uniqueness is enforced by constraints**, mapped to specific 409s, and a
  conflict rolls back only its SAVEPOINT — no half-made account survives, and
  the request's transaction stays usable.
* **Deactivation ends sessions.** After it, the account's old refresh token no
  longer rotates, its access token no longer authenticates, and it cannot log
  in. Self-deactivation is refused.
* **PostGIS has the last word on zone geometry.** A self-intersecting polygon
  is a clean 400 carrying PostGIS's reason, not a 500.

Module skips cleanly when no database is reachable, matching `test_auth.py`.
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
from app.core.security import create_access_token, verify_password
from app.models.authority_user import AuthorityUser
from app.models.authority_zone import AuthorityZone
from app.models.department import Department
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.models.zone import Zone

PASSWORD = "initial-password-123"

# A square around central Bengaluru, as GeoJSON ([lng, lat]); (LAT, LNG) is inside it.
LAT, LNG = 12.9716, 77.5946
SQUARE = [[[77.57, 12.95], [77.61, 12.95], [77.61, 12.99], [77.57, 12.99], [77.57, 12.95]]]
# Classic bow-tie: the edges (0,0)-(1,1) and (1,0)-(0,1) cross at (0.5, 0.5).
BOWTIE = [[[0.0, 0.0], [1.0, 1.0], [1.0, 0.0], [0.0, 1.0], [0.0, 0.0]]]

CREDENTIAL_MARKERS = ("password", "hash", "token", "oauth")


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
            "start Postgres (docker compose up -d db) to run the admin integration tests.",
        )


@pytest.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """One rolled-back transaction per test, on a loop-private engine.

    Same reasoning as `test_auth.py`: pytest-asyncio gives each test its own
    event loop, and a pooled connection reused across loops makes asyncpg raise
    "got Future attached to a different loop".
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


def _uid() -> str:
    return uuid.uuid4().hex[:10]


async def _user(db: AsyncSession, role: str = "CITIZEN") -> User:
    user = User(
        email=f"{role.lower()}-{_uid()}@example.com",
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


@pytest.fixture
async def admin(db_session: AsyncSession) -> User:
    return await _user(db_session, "ADMIN")


@pytest.fixture
def admin_headers(admin: User) -> dict[str, str]:
    return _auth(admin)


async def _department(db: AsyncSession, code: str = "PWD") -> Department:
    """One of the six departments migration 014 seeds."""
    department = await db.scalar(select(Department).where(Department.code == code))
    assert department is not None, "migration 014 should have seeded this department"
    return department


async def _zone_id(client: AsyncClient, headers: dict[str, str], name: str | None = None) -> uuid.UUID:
    response = await client.post(
        "/v1/admin/zones",
        json={"name": name or f"Zone {_uid()}", "boundary": {"type": "Polygon", "coordinates": SQUARE}},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["id"])


def _authority_body(department_id: uuid.UUID, **overrides) -> dict:
    return {
        "email": f"officer-{_uid()}@municipality.gov.in",
        "name": "Ravi Kumar",
        "password": PASSWORD,
        "employee_id": f"EMP-{_uid()}",
        "department_id": str(department_id),
        "designation": "Field Supervisor",
        **overrides,
    }


async def _create_authority(client: AsyncClient, headers: dict[str, str], department_id: uuid.UUID, **overrides):
    return await client.post(
        "/v1/admin/authority-users", json=_authority_body(department_id, **overrides), headers=headers
    )


def _assert_no_credentials(payload: dict) -> None:
    leaked = [key for key in payload if any(marker in key.lower() for marker in CREDENTIAL_MARKERS)]
    assert not leaked, f"response exposes credential-like field(s): {leaked}"


# ── Access control ──────────────────────────────────────────────────────

ENDPOINTS = [
    ("get", "/v1/admin/authority-users"),
    ("post", "/v1/admin/authority-users"),
    ("patch", f"/v1/admin/authority-users/{uuid.uuid4()}/deactivate"),
    ("get", "/v1/admin/departments"),
    ("post", "/v1/admin/departments"),
    ("patch", f"/v1/admin/departments/{uuid.uuid4()}"),
    ("get", "/v1/admin/zones"),
    ("post", "/v1/admin/zones"),
    ("get", "/v1/admin/system/stats"),
]
ENDPOINT_IDS = [f"{m.upper()} {p}" for m, p in ENDPOINTS]


class TestAccessControl:
    @pytest.mark.parametrize(("method", "path"), ENDPOINTS, ids=ENDPOINT_IDS)
    async def test_unauthenticated_is_401(self, client: AsyncClient, method: str, path: str) -> None:
        response = await client.request(method, path, json={})
        assert response.status_code == 401, response.text

    @pytest.mark.parametrize("role", ["CITIZEN", "AUTHORITY"])
    @pytest.mark.parametrize(("method", "path"), ENDPOINTS, ids=ENDPOINT_IDS)
    async def test_non_admin_is_403(
        self, client: AsyncClient, db_session: AsyncSession, role: str, method: str, path: str
    ) -> None:
        """Checked before the body is acted on: a 403 even for a well-formed request."""
        user = await _user(db_session, role)
        response = await client.request(method, path, json={}, headers=_auth(user))
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "FORBIDDEN"

    async def test_deactivated_admin_is_401(self, client: AsyncClient, db_session: AsyncSession) -> None:
        """A valid ADMIN token on a deactivated account opens nothing."""
        admin = await _user(db_session, "ADMIN")
        admin.is_active = False
        await db_session.flush()
        response = await client.get("/v1/admin/departments", headers=_auth(admin))
        assert response.status_code == 401


# ── Creating authority accounts ─────────────────────────────────────────


class TestCreateAuthorityUser:
    async def test_creates_user_profile_and_zone_links(
        self, client: AsyncClient, db_session: AsyncSession, admin_headers: dict[str, str]
    ) -> None:
        department = await _department(db_session)
        zone_a = await _zone_id(client, admin_headers)
        zone_b = await _zone_id(client, admin_headers)

        response = await _create_authority(
            client, admin_headers, department.id, zone_ids=[str(zone_a), str(zone_b), str(zone_a)]
        )
        assert response.status_code == 201, response.text
        body = response.json()

        assert body["role"] == "AUTHORITY"
        assert body["is_active"] is True
        assert body["department_id"] == str(department.id)
        assert body["department_name"] == department.name
        assert body["is_dept_admin"] is False
        assert sorted(body["zone_ids"]) == sorted([str(zone_a), str(zone_b)]), "duplicates must collapse"
        _assert_no_credentials(body)

        user = await db_session.get(User, uuid.UUID(body["user_id"]))
        assert user is not None
        assert user.role == "AUTHORITY"
        assert user.password_hash != PASSWORD, "the password must be stored hashed"
        assert verify_password(PASSWORD, user.password_hash)

        profile = await db_session.get(AuthorityUser, uuid.UUID(body["id"]))
        assert profile is not None
        assert profile.user_id == user.id
        links = await db_session.scalar(
            select(func.count()).select_from(AuthorityZone).where(AuthorityZone.authority_user_id == profile.id)
        )
        assert links == 2

    async def test_new_account_can_log_in(
        self, client: AsyncClient, db_session: AsyncSession, admin_headers: dict[str, str]
    ) -> None:
        """End to end: the admin-supplied password is the one that works."""
        department = await _department(db_session)
        body = _authority_body(department.id)
        assert (await client.post("/v1/admin/authority-users", json=body, headers=admin_headers)).status_code == 201

        login = await client.post("/v1/auth/login", json={"email": body["email"], "password": PASSWORD})
        assert login.status_code == 200, login.text
        assert login.json()["user"]["role"] == "AUTHORITY"

    async def test_email_is_normalised_to_lowercase(
        self, client: AsyncClient, db_session: AsyncSession, admin_headers: dict[str, str]
    ) -> None:
        department = await _department(db_session)
        response = await _create_authority(client, admin_headers, department.id, email=f"  Officer.{_uid()}@Gov.IN ")
        assert response.status_code == 201, response.text
        assert response.json()["email"] == response.json()["email"].lower().strip()

    async def test_records_the_provisioning_admins_profile(
        self, client: AsyncClient, db_session: AsyncSession, admin: User, admin_headers: dict[str, str]
    ) -> None:
        department = await _department(db_session)
        admin_profile = AuthorityUser(user_id=admin.id, department_id=department.id, employee_id=f"ADM-{_uid()}")
        db_session.add(admin_profile)
        await db_session.flush()

        response = await _create_authority(client, admin_headers, department.id)
        assert response.status_code == 201, response.text
        profile = await db_session.get(AuthorityUser, uuid.UUID(response.json()["id"]))
        assert profile is not None
        assert profile.created_by == admin_profile.id

    async def test_admin_without_a_profile_leaves_created_by_null(
        self, client: AsyncClient, db_session: AsyncSession, admin_headers: dict[str, str]
    ) -> None:
        department = await _department(db_session)
        response = await _create_authority(client, admin_headers, department.id)
        profile = await db_session.get(AuthorityUser, uuid.UUID(response.json()["id"]))
        assert profile is not None
        assert profile.created_by is None

    async def test_duplicate_email_is_409(
        self, client: AsyncClient, db_session: AsyncSession, admin_headers: dict[str, str]
    ) -> None:
        department = await _department(db_session)
        email = f"dup-{_uid()}@example.com"
        assert (await _create_authority(client, admin_headers, department.id, email=email)).status_code == 201

        response = await _create_authority(client, admin_headers, department.id, email=email.upper())
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "EMAIL_ALREADY_REGISTERED"

    async def test_email_of_an_existing_citizen_is_409(
        self, client: AsyncClient, db_session: AsyncSession, admin_headers: dict[str, str]
    ) -> None:
        """One account per email across every role — a citizen cannot be silently promoted."""
        citizen = await _user(db_session, "CITIZEN")
        department = await _department(db_session)
        response = await _create_authority(client, admin_headers, department.id, email=citizen.email)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "EMAIL_ALREADY_REGISTERED"

    async def test_duplicate_employee_id_is_409_and_leaves_no_orphan_user(
        self, client: AsyncClient, db_session: AsyncSession, admin_headers: dict[str, str]
    ) -> None:
        """The users row is flushed before the profile collides; the SAVEPOINT must take it back out."""
        department = await _department(db_session)
        employee_id = f"EMP-{_uid()}"
        assert (
            await _create_authority(client, admin_headers, department.id, employee_id=employee_id)
        ).status_code == 201

        orphan_email = f"orphan-{_uid()}@example.com"
        response = await _create_authority(
            client, admin_headers, department.id, employee_id=employee_id, email=orphan_email
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "EMPLOYEE_ID_ALREADY_EXISTS"
        assert await db_session.scalar(select(User.id).where(User.email == orphan_email)) is None

    async def test_transaction_survives_a_conflict(
        self, client: AsyncClient, db_session: AsyncSession, admin_headers: dict[str, str]
    ) -> None:
        """A 409 must roll back its savepoint only, not everything the request has done."""
        department = await _department(db_session)
        email = f"dup-{_uid()}@example.com"
        await _create_authority(client, admin_headers, department.id, email=email)
        assert (await _create_authority(client, admin_headers, department.id, email=email)).status_code == 409
        assert (await _create_authority(client, admin_headers, department.id)).status_code == 201

    @pytest.mark.parametrize("password", ["", "short", "1234567"])
    async def test_password_below_registration_policy_is_422(
        self, client: AsyncClient, db_session: AsyncSession, admin_headers: dict[str, str], password: str
    ) -> None:
        department = await _department(db_session)
        response = await _create_authority(client, admin_headers, department.id, password=password)
        assert response.status_code == 422

    async def test_password_is_required(
        self, client: AsyncClient, db_session: AsyncSession, admin_headers: dict[str, str]
    ) -> None:
        department = await _department(db_session)
        body = _authority_body(department.id)
        del body["password"]
        response = await client.post("/v1/admin/authority-users", json=body, headers=admin_headers)
        assert response.status_code == 422

    async def test_unknown_department_is_404(self, client: AsyncClient, admin_headers: dict[str, str]) -> None:
        response = await _create_authority(client, admin_headers, uuid.uuid4())
        assert response.status_code == 404

    async def test_inactive_department_is_400(
        self, client: AsyncClient, db_session: AsyncSession, admin_headers: dict[str, str]
    ) -> None:
        department = await _department(db_session, "PRK")
        department.is_active = False
        await db_session.flush()
        response = await _create_authority(client, admin_headers, department.id)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "DEPARTMENT_INACTIVE"

    async def test_unknown_zone_is_404_naming_it(
        self, client: AsyncClient, db_session: AsyncSession, admin_headers: dict[str, str]
    ) -> None:
        department = await _department(db_session)
        real = await _zone_id(client, admin_headers)
        ghost = uuid.uuid4()
        response = await _create_authority(client, admin_headers, department.id, zone_ids=[str(real), str(ghost)])
        assert response.status_code == 404
        assert response.json()["error"]["details"]["missing_zone_ids"] == [str(ghost)]


# ── Listing authority accounts ──────────────────────────────────────────


class TestListAuthorityUsers:
    async def test_lists_with_both_identifiers_and_no_credentials(
        self, client: AsyncClient, db_session: AsyncSession, admin_headers: dict[str, str]
    ) -> None:
        department = await _department(db_session)
        created = (await _create_authority(client, admin_headers, department.id)).json()

        response = await client.get(
            "/v1/admin/authority-users", params={"department_id": str(department.id)}, headers=admin_headers
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert {"items", "total", "page", "page_size", "total_pages"} <= set(body)

        match = next(item for item in body["items"] if item["id"] == created["id"])
        assert match["user_id"] == created["user_id"]
        for item in body["items"]:
            _assert_no_credentials(item)

    async def test_filters_by_department(
        self, client: AsyncClient, db_session: AsyncSession, admin_headers: dict[str, str]
    ) -> None:
        pwd = await _department(db_session, "PWD")
        san = await _department(db_session, "SAN")
        in_pwd = (await _create_authority(client, admin_headers, pwd.id)).json()
        in_san = (await _create_authority(client, admin_headers, san.id)).json()

        response = await client.get(
            "/v1/admin/authority-users", params={"department_id": str(san.id), "page_size": 100}, headers=admin_headers
        )
        ids = {item["id"] for item in response.json()["items"]}
        assert in_san["id"] in ids
        assert in_pwd["id"] not in ids
        assert all(item["department_id"] == str(san.id) for item in response.json()["items"])

    async def test_filters_by_active_status(
        self, client: AsyncClient, db_session: AsyncSession, admin_headers: dict[str, str]
    ) -> None:
        department = await _department(db_session)
        active = (await _create_authority(client, admin_headers, department.id)).json()
        gone = (await _create_authority(client, admin_headers, department.id)).json()
        await client.patch(f"/v1/admin/authority-users/{gone['user_id']}/deactivate", headers=admin_headers)

        params = {"department_id": str(department.id), "page_size": 100}
        only_active = await client.get(
            "/v1/admin/authority-users", params={**params, "is_active": "true"}, headers=admin_headers
        )
        only_inactive = await client.get(
            "/v1/admin/authority-users", params={**params, "is_active": "false"}, headers=admin_headers
        )

        active_ids = {item["id"] for item in only_active.json()["items"]}
        inactive_ids = {item["id"] for item in only_inactive.json()["items"]}
        assert active["id"] in active_ids
        assert gone["id"] not in active_ids
        assert gone["id"] in inactive_ids
        assert active["id"] not in inactive_ids

    async def test_paginates_and_orders_by_name(
        self, client: AsyncClient, db_session: AsyncSession, admin_headers: dict[str, str]
    ) -> None:
        department = await _department(db_session, "ELE")
        for name in ("Charlie", "Alice", "Bob"):
            assert (await _create_authority(client, admin_headers, department.id, name=name)).status_code == 201

        params = {"department_id": str(department.id), "page_size": 2}
        first = (await client.get("/v1/admin/authority-users", params=params, headers=admin_headers)).json()
        second = (
            await client.get("/v1/admin/authority-users", params={**params, "page": 2}, headers=admin_headers)
        ).json()

        assert first["total"] == 3
        assert first["total_pages"] == 2
        assert [i["name"] for i in first["items"]] == ["Alice", "Bob"]
        assert [i["name"] for i in second["items"]] == ["Charlie"]

    async def test_page_size_is_capped(self, client: AsyncClient, admin_headers: dict[str, str]) -> None:
        response = await client.get("/v1/admin/authority-users", params={"page_size": 101}, headers=admin_headers)
        assert response.status_code == 422


# ── Deactivation ────────────────────────────────────────────────────────


class TestDeactivateAuthorityUser:
    async def _provision_and_log_in(
        self, client: AsyncClient, db_session: AsyncSession, admin_headers: dict[str, str]
    ) -> tuple[dict, dict]:
        """Create an authority account and open a session on it. Returns (account, login body)."""
        department = await _department(db_session)
        body = _authority_body(department.id)
        account = (await client.post("/v1/admin/authority-users", json=body, headers=admin_headers)).json()
        login = await client.post("/v1/auth/login", json={"email": body["email"], "password": PASSWORD})
        assert login.status_code == 200, login.text
        return {**account, "email": body["email"]}, login.json()

    async def test_deactivates_and_reports_it(
        self, client: AsyncClient, db_session: AsyncSession, admin_headers: dict[str, str]
    ) -> None:
        account, _ = await self._provision_and_log_in(client, db_session, admin_headers)
        response = await client.patch(
            f"/v1/admin/authority-users/{account['user_id']}/deactivate", headers=admin_headers
        )
        assert response.status_code == 200, response.text
        assert response.json()["is_active"] is False
        assert response.json()["user_id"] == account["user_id"]

        user = await db_session.get(User, uuid.UUID(account["user_id"]))
        assert user is not None
        assert user.is_active is False
        assert await db_session.get(AuthorityUser, uuid.UUID(account["id"])) is not None, "deactivate, never delete"

    async def test_old_refresh_token_no_longer_rotates(
        self, client: AsyncClient, db_session: AsyncSession, admin_headers: dict[str, str]
    ) -> None:
        account, session = await self._provision_and_log_in(client, db_session, admin_headers)
        await client.patch(f"/v1/admin/authority-users/{account['user_id']}/deactivate", headers=admin_headers)

        response = await client.post("/v1/auth/refresh", json={"refresh_token": session["refresh_token"]})
        assert response.status_code == 401

    async def test_every_refresh_token_is_revoked_in_the_database(
        self, client: AsyncClient, db_session: AsyncSession, admin_headers: dict[str, str]
    ) -> None:
        """Not merely refused by the is_active check — actually revoked, on every device."""
        account, _ = await self._provision_and_log_in(client, db_session, admin_headers)
        second = await client.post("/v1/auth/login", json={"email": account["email"], "password": PASSWORD})
        assert second.status_code == 200

        await client.patch(f"/v1/admin/authority-users/{account['user_id']}/deactivate", headers=admin_headers)

        live = await db_session.scalar(
            select(func.count())
            .select_from(RefreshToken)
            .where(RefreshToken.user_id == uuid.UUID(account["user_id"]), RefreshToken.is_revoked.is_(False))
        )
        assert live == 0

    async def test_other_accounts_sessions_are_untouched(
        self, client: AsyncClient, db_session: AsyncSession, admin_headers: dict[str, str]
    ) -> None:
        target, _ = await self._provision_and_log_in(client, db_session, admin_headers)
        _, bystander_session = await self._provision_and_log_in(client, db_session, admin_headers)

        await client.patch(f"/v1/admin/authority-users/{target['user_id']}/deactivate", headers=admin_headers)

        response = await client.post("/v1/auth/refresh", json={"refresh_token": bystander_session["refresh_token"]})
        assert response.status_code == 200, response.text

    async def test_access_token_and_login_stop_working(
        self, client: AsyncClient, db_session: AsyncSession, admin_headers: dict[str, str]
    ) -> None:
        account, session = await self._provision_and_log_in(client, db_session, admin_headers)
        await client.patch(f"/v1/admin/authority-users/{account['user_id']}/deactivate", headers=admin_headers)

        me = await client.get("/v1/users/me", headers={"Authorization": f"Bearer {session['access_token']}"})
        assert me.status_code == 401
        login = await client.post("/v1/auth/login", json={"email": account["email"], "password": PASSWORD})
        assert login.status_code == 401

    async def test_is_idempotent(
        self, client: AsyncClient, db_session: AsyncSession, admin_headers: dict[str, str]
    ) -> None:
        account, _ = await self._provision_and_log_in(client, db_session, admin_headers)
        path = f"/v1/admin/authority-users/{account['user_id']}/deactivate"
        first = await client.patch(path, headers=admin_headers)
        again = await client.patch(path, headers=admin_headers)
        assert first.status_code == again.status_code == 200
        assert again.json() == first.json()

    async def test_self_deactivation_is_refused(
        self, client: AsyncClient, db_session: AsyncSession, admin: User, admin_headers: dict[str, str]
    ) -> None:
        """Refused even when the admin has an authority profile and so is a valid target."""
        department = await _department(db_session)
        db_session.add(AuthorityUser(user_id=admin.id, department_id=department.id, employee_id=f"ADM-{_uid()}"))
        await db_session.flush()

        response = await client.patch(f"/v1/admin/authority-users/{admin.id}/deactivate", headers=admin_headers)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "CANNOT_DEACTIVATE_SELF"

        await db_session.refresh(admin)
        assert admin.is_active is True
        assert (await client.get("/v1/admin/departments", headers=admin_headers)).status_code == 200

    async def test_citizen_is_not_a_target(
        self, client: AsyncClient, db_session: AsyncSession, admin_headers: dict[str, str]
    ) -> None:
        citizen = await _user(db_session, "CITIZEN")
        response = await client.patch(f"/v1/admin/authority-users/{citizen.id}/deactivate", headers=admin_headers)
        assert response.status_code == 404

        await db_session.refresh(citizen)
        assert citizen.is_active is True

    async def test_unknown_user_is_404(self, client: AsyncClient, admin_headers: dict[str, str]) -> None:
        response = await client.patch(f"/v1/admin/authority-users/{uuid.uuid4()}/deactivate", headers=admin_headers)
        assert response.status_code == 404

    async def test_profile_id_is_not_accepted_in_place_of_user_id(
        self, client: AsyncClient, db_session: AsyncSession, admin_headers: dict[str, str]
    ) -> None:
        """The path takes the *user* id; a profile id must not deactivate anything by coincidence."""
        account, _ = await self._provision_and_log_in(client, db_session, admin_headers)
        response = await client.patch(f"/v1/admin/authority-users/{account['id']}/deactivate", headers=admin_headers)
        assert response.status_code == 404


# ── Departments ─────────────────────────────────────────────────────────


class TestDepartments:
    async def test_lists_the_seeded_departments_with_their_settings(
        self, client: AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        response = await client.get("/v1/admin/departments", headers=admin_headers)
        assert response.status_code == 200, response.text
        items = {item["code"]: item for item in response.json()["items"]}

        assert {"PWD", "SAN", "WSD", "ELE", "PRK", "GEN"} <= set(items)
        assert items["SAN"]["sla_hours"] == 24
        assert items["SAN"]["upvote_alert_threshold"] == 15
        assert "POTHOLE" in items["PWD"]["categories"]
        codes = [item["code"] for item in response.json()["items"]]
        assert codes == sorted(codes)

    async def test_creates_a_department(self, client: AsyncClient, admin_headers: dict[str, str]) -> None:
        code = f"T{_uid()[:6]}".lower()
        response = await client.post(
            "/v1/admin/departments",
            json={"name": f"Storm Drains {code}", "code": code, "sla_hours": 36, "upvote_alert_threshold": 4},
            headers=admin_headers,
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["code"] == code.upper(), "codes are stored uppercased"
        assert body["sla_hours"] == 36
        assert body["upvote_alert_threshold"] == 4
        assert body["is_active"] is True
        assert body["categories"] == [], "a new department routes nothing until mapped"

    async def test_defaults_match_the_schema(self, client: AsyncClient, admin_headers: dict[str, str]) -> None:
        response = await client.post(
            "/v1/admin/departments", json={"name": f"Dept {_uid()}", "code": f"D{_uid()[:6]}"}, headers=admin_headers
        )
        assert response.status_code == 201, response.text
        assert response.json()["sla_hours"] == 72
        assert response.json()["upvote_alert_threshold"] == 10

    async def test_duplicate_name_is_409_case_insensitively(
        self, client: AsyncClient, db_session: AsyncSession, admin_headers: dict[str, str]
    ) -> None:
        existing = await _department(db_session, "SAN")
        response = await client.post(
            "/v1/admin/departments",
            json={"name": f"  {existing.name.upper()} ", "code": f"N{_uid()[:6]}"},
            headers=admin_headers,
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "DEPARTMENT_NAME_EXISTS"

    async def test_duplicate_code_is_409(self, client: AsyncClient, admin_headers: dict[str, str]) -> None:
        response = await client.post(
            "/v1/admin/departments", json={"name": f"Another {_uid()}", "code": "pwd"}, headers=admin_headers
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "DEPARTMENT_CODE_EXISTS"

    async def test_transaction_survives_a_code_conflict(
        self, client: AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        conflict = await client.post(
            "/v1/admin/departments", json={"name": f"Clash {_uid()}", "code": "SAN"}, headers=admin_headers
        )
        assert conflict.status_code == 409
        ok = await client.post(
            "/v1/admin/departments", json={"name": f"Fine {_uid()}", "code": f"F{_uid()[:6]}"}, headers=admin_headers
        )
        assert ok.status_code == 201, ok.text

    @pytest.mark.parametrize(
        "overrides",
        [
            {"sla_hours": 0},
            {"upvote_alert_threshold": 0},
            {"upvote_alert_threshold": -5},
            {"code": "has space"},
            {"code": "X" * 21},
            {"name": "   "},
        ],
        ids=["sla-0", "threshold-0", "threshold-negative", "code-pattern", "code-too-long", "blank-name"],
    )
    async def test_invalid_create_is_422(
        self, client: AsyncClient, admin_headers: dict[str, str], overrides: dict
    ) -> None:
        body = {"name": f"Bad {_uid()}", "code": f"B{_uid()[:6]}", **overrides}
        response = await client.post("/v1/admin/departments", json=body, headers=admin_headers)
        assert response.status_code == 422


class TestUpdateDepartment:
    async def test_updates_threshold_and_leaves_sla_alone(
        self, client: AsyncClient, db_session: AsyncSession, admin_headers: dict[str, str]
    ) -> None:
        department = await _department(db_session, "WSD")
        sla_before = department.sla_hours

        response = await client.patch(
            f"/v1/admin/departments/{department.id}", json={"upvote_alert_threshold": 3}, headers=admin_headers
        )
        assert response.status_code == 200, response.text
        assert response.json()["upvote_alert_threshold"] == 3
        assert response.json()["sla_hours"] == sla_before

        stored = await db_session.scalar(
            select(Department.upvote_alert_threshold).where(Department.id == department.id)
        )
        assert stored == 3

    async def test_updates_both(
        self, client: AsyncClient, db_session: AsyncSession, admin_headers: dict[str, str]
    ) -> None:
        department = await _department(db_session, "GEN")
        response = await client.patch(
            f"/v1/admin/departments/{department.id}",
            json={"sla_hours": 1, "upvote_alert_threshold": 1},
            headers=admin_headers,
        )
        assert response.status_code == 200, response.text
        assert (response.json()["sla_hours"], response.json()["upvote_alert_threshold"]) == (1, 1)

    @pytest.mark.parametrize(
        "body",
        [
            {"upvote_alert_threshold": 0},
            {"upvote_alert_threshold": -1},
            {"sla_hours": 0},
            {"upvote_alert_threshold": None},
            {"sla_hours": None, "upvote_alert_threshold": 5},
            {},
            {"upvote_alert_threshold": 2**31},
        ],
        ids=["threshold-0", "threshold-negative", "sla-0", "threshold-null", "sla-null", "empty", "int32-overflow"],
    )
    async def test_invalid_update_is_422_and_changes_nothing(
        self, client: AsyncClient, db_session: AsyncSession, admin_headers: dict[str, str], body: dict
    ) -> None:
        department = await _department(db_session, "PWD")
        before = (department.sla_hours, department.upvote_alert_threshold)

        response = await client.patch(f"/v1/admin/departments/{department.id}", json=body, headers=admin_headers)
        assert response.status_code == 422

        row = (
            await db_session.execute(
                select(Department.sla_hours, Department.upvote_alert_threshold).where(Department.id == department.id)
            )
        ).one()
        assert tuple(row) == before

    async def test_unknown_department_is_404(self, client: AsyncClient, admin_headers: dict[str, str]) -> None:
        response = await client.patch(
            f"/v1/admin/departments/{uuid.uuid4()}", json={"sla_hours": 10}, headers=admin_headers
        )
        assert response.status_code == 404


# ── Zones ───────────────────────────────────────────────────────────────


class TestZones:
    async def test_creates_a_zone_and_round_trips_its_boundary(
        self, client: AsyncClient, db_session: AsyncSession, admin_headers: dict[str, str]
    ) -> None:
        department = await _department(db_session)
        response = await client.post(
            "/v1/admin/zones",
            json={
                "name": f"Ward {_uid()}",
                "department_id": str(department.id),
                "boundary": {"type": "Polygon", "coordinates": SQUARE},
            },
            headers=admin_headers,
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["department_id"] == str(department.id)
        assert body["is_active"] is True
        assert body["boundary"] == {"type": "Polygon", "coordinates": SQUARE}

        srid = await db_session.scalar(select(func.ST_SRID(Zone.boundary)).where(Zone.id == uuid.UUID(body["id"])))
        assert srid == 4326

    async def test_new_zone_is_used_by_issue_auto_assignment(
        self, client: AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        """The point of a zone: a report inside it is routed to it at submission."""
        # A unique, tiny square far from every other test's zones, so only this
        # one can contain the point.
        lng, lat = -60.5, -30.5
        ring = [[lng - 0.01, lat - 0.01], [lng + 0.01, lat - 0.01], [lng + 0.01, lat + 0.01], [lng - 0.01, lat + 0.01]]
        ring.append(ring[0])
        zone = await client.post(
            "/v1/admin/zones",
            json={"name": f"Remote {_uid()}", "boundary": {"type": "Polygon", "coordinates": [ring]}},
            headers=admin_headers,
        )
        assert zone.status_code == 201, zone.text

        issue = await client.post(
            "/v1/issues", data={"category": "POTHOLE", "latitude": str(lat), "longitude": str(lng)}
        )
        assert issue.status_code == 201, issue.text
        assert issue.json()["zone_id"] == zone.json()["id"]

    async def test_polygon_with_a_hole_is_accepted(self, client: AsyncClient, admin_headers: dict[str, str]) -> None:
        hole = [[77.58, 12.96], [77.59, 12.96], [77.59, 12.97], [77.58, 12.97], [77.58, 12.96]]
        response = await client.post(
            "/v1/admin/zones",
            json={"name": f"Holed {_uid()}", "boundary": {"type": "Polygon", "coordinates": [SQUARE[0], hole]}},
            headers=admin_headers,
        )
        assert response.status_code == 201, response.text
        assert len(response.json()["boundary"]["coordinates"]) == 2

    async def test_self_intersecting_polygon_is_a_clean_400(
        self, client: AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        response = await client.post(
            "/v1/admin/zones",
            json={"name": f"Bowtie {_uid()}", "boundary": {"type": "Polygon", "coordinates": BOWTIE}},
            headers=admin_headers,
        )
        assert response.status_code == 400, response.text
        error = response.json()["error"]
        assert error["code"] == "INVALID_GEOMETRY"
        assert "Self-intersection" in error["details"]["reason"]

    async def test_transaction_survives_an_invalid_polygon(
        self, client: AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        bad = await client.post(
            "/v1/admin/zones",
            json={"name": f"Bowtie {_uid()}", "boundary": {"type": "Polygon", "coordinates": BOWTIE}},
            headers=admin_headers,
        )
        assert bad.status_code == 400
        await _zone_id(client, admin_headers)

    @pytest.mark.parametrize(
        ("coordinates", "fragment"),
        [
            ([], "exterior ring"),
            ([[[77.57, 12.95], [77.61, 12.95], [77.61, 12.99], [77.57, 12.99]]], "not closed"),
            ([[[77.57, 12.95], [77.61, 12.95], [77.57, 12.95]]], "at least 4"),
            ([[[77.57, 12.95], [77.61, 12.95], [77.61, 95.0], [77.57, 12.99], [77.57, 12.95]]], "out of range"),
            ([[[12.95, 77.57], [12.95, 181.0], [12.99, 77.61], [12.95, 77.57]]], "out of range"),
            ([[[77.57, 12.95, 10.0], [77.61, 12.95, 10.0], [77.61, 12.99, 10.0], [77.57, 12.95, 10.0]]], "[longitude"),
            ([[[0.0, 0.0], [1.0, 0.0], [2.0, 0.0], [0.0, 0.0]]], "Self-intersection"),
        ],
        ids=["no-rings", "unclosed", "too-few-positions", "latitude-range", "longitude-range", "3d", "zero-area"],
    )
    async def test_malformed_polygon_is_400_with_a_reason(
        self, client: AsyncClient, admin_headers: dict[str, str], coordinates: list, fragment: str
    ) -> None:
        response = await client.post(
            "/v1/admin/zones",
            json={"name": f"Bad {_uid()}", "boundary": {"type": "Polygon", "coordinates": coordinates}},
            headers=admin_headers,
        )
        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == "INVALID_GEOMETRY"
        assert fragment in response.json()["error"]["details"]["reason"]

    @pytest.mark.parametrize(
        "boundary",
        [
            {"type": "Point", "coordinates": [77.5, 12.9]},
            {"type": "MultiPolygon", "coordinates": [SQUARE]},
            {"type": "Polygon", "coordinates": "not a list"},
            {"type": "Polygon", "coordinates": [[["a", "b"]]]},
            {"coordinates": SQUARE},
        ],
        ids=["point", "multipolygon", "string-coordinates", "non-numeric", "missing-type"],
    )
    async def test_wrong_geojson_shape_is_422(
        self, client: AsyncClient, admin_headers: dict[str, str], boundary: dict
    ) -> None:
        response = await client.post(
            "/v1/admin/zones", json={"name": f"Bad {_uid()}", "boundary": boundary}, headers=admin_headers
        )
        assert response.status_code == 422

    async def test_unknown_department_is_404(self, client: AsyncClient, admin_headers: dict[str, str]) -> None:
        response = await client.post(
            "/v1/admin/zones",
            json={
                "name": f"Orphan {_uid()}",
                "department_id": str(uuid.uuid4()),
                "boundary": {"type": "Polygon", "coordinates": SQUARE},
            },
            headers=admin_headers,
        )
        assert response.status_code == 404

    async def test_lists_zones_with_boundaries(self, client: AsyncClient, admin_headers: dict[str, str]) -> None:
        name = f"Listed {_uid()}"
        zone_id = await _zone_id(client, admin_headers, name=name)
        response = await client.get("/v1/admin/zones", headers=admin_headers)
        assert response.status_code == 200, response.text
        match = next(item for item in response.json()["items"] if item["id"] == str(zone_id))
        assert match["name"] == name
        assert match["boundary"]["type"] == "Polygon"


# ── System stats ────────────────────────────────────────────────────────


class TestSystemStats:
    async def test_counts_match_the_database(
        self, client: AsyncClient, db_session: AsyncSession, admin_headers: dict[str, str]
    ) -> None:
        department = await _department(db_session)
        await _create_authority(client, admin_headers, department.id)
        await _zone_id(client, admin_headers)

        response = await client.get("/v1/admin/system/stats", headers=admin_headers)
        assert response.status_code == 200, response.text
        stats = response.json()

        async def count(sql: str) -> int:
            return (await db_session.execute(text(sql))).scalar_one()

        assert stats == {
            "total_users": await count("SELECT COUNT(*) FROM users"),
            "total_issues": await count("SELECT COUNT(*) FROM issues"),
            "total_resolved": await count("SELECT COUNT(*) FROM issues WHERE status = 'RESOLVED'"),
            "active_authority_users": await count(
                "SELECT COUNT(*) FROM authority_users a JOIN users u ON u.id = a.user_id WHERE u.is_active"
            ),
            "departments": await count("SELECT COUNT(*) FROM departments WHERE is_active"),
            "zones": await count("SELECT COUNT(*) FROM zones WHERE is_active"),
        }

    async def test_counts_move_with_writes(
        self, client: AsyncClient, db_session: AsyncSession, admin_headers: dict[str, str]
    ) -> None:
        before = (await client.get("/v1/admin/system/stats", headers=admin_headers)).json()
        department = await _department(db_session)
        await _create_authority(client, admin_headers, department.id)
        await _zone_id(client, admin_headers)
        after = (await client.get("/v1/admin/system/stats", headers=admin_headers)).json()

        assert after["total_users"] == before["total_users"] + 1
        assert after["active_authority_users"] == before["active_authority_users"] + 1
        assert after["zones"] == before["zones"] + 1
