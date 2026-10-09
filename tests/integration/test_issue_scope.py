"""Zone-scoped triage, assignee eligibility and the staff picker, against a live database.

The authorisation gap these close: any AUTHORITY could change the status of, or
assign, any issue in the city. TRD §RBAC grants an authority the issues *in
their zone*. What is proven here:

* **An authority acts only inside their zones.** A zone-B issue is a 404 to a
  zone-A authority on every staff endpoint — status, assign, assignable-staff.
* **The 404 is indistinguishable from a missing issue** — same status, same
  bytes — and it comes before every other check, so a status-machine 400 or an
  assignee 400 cannot confirm the issue exists either.
* **An issue outside every zone is ADMIN-only.**
* **An issue is never assigned to someone who cannot see it**, and the picker
  lists exactly the people the assign endpoint accepts.
* **The public reads stay public.** The citizen map depends on them.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import NullPool, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings
from app.core.security import create_access_token
from app.models.issue import Issue
from app.models.issue_status_history import IssueStatusHistory
from app.models.user import User
from tests.integration.staff_helpers import staff_user, zone_around

# Two zones far enough apart that their ~1 km squares cannot overlap, and a
# point outside both.
A_LAT, A_LNG = 12.9716, 77.5946
B_LAT, B_LNG = 13.1000, 77.7000
NOWHERE_LAT, NOWHERE_LNG = 1.0, 1.0


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
            "start Postgres (docker compose up -d db) to run the issue-scope integration tests.",
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


def _auth(user: User) -> dict[str, str]:
    token = create_access_token(user_id=str(user.id), role=user.role, email=user.email)
    return {"Authorization": f"Bearer {token}"}


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


async def _submit(client: AsyncClient, latitude: float, longitude: float) -> str:
    response = await client.post(
        "/v1/issues",
        data={"category": "POTHOLE", "latitude": str(latitude), "longitude": str(longitude)},
    )
    assert response.status_code == 201, response.text
    return response.json()["issue_id"]


class City:
    """Two zones, an issue in each, one outside both, and a zone-A authority."""

    zone_a: uuid.UUID
    zone_b: uuid.UUID
    issue_a: str
    issue_b: str
    issue_nowhere: str
    authority_a: User


@pytest.fixture
async def city(client: AsyncClient, db_session: AsyncSession) -> City:
    world = City()
    world.zone_a = await zone_around(db_session, A_LAT, A_LNG, "Zone A")
    world.zone_b = await zone_around(db_session, B_LAT, B_LNG, "Zone B")
    world.issue_a = await _submit(client, A_LAT, A_LNG)
    world.issue_b = await _submit(client, B_LAT, B_LNG)
    world.issue_nowhere = await _submit(client, NOWHERE_LAT, NOWHERE_LNG)
    world.authority_a, _ = await staff_user(db_session, [world.zone_a])

    # Routing must have put each issue where the test assumes it is.
    zones = dict(
        (
            await db_session.execute(
                select(Issue.id, Issue.zone_id).where(
                    Issue.id.in_([uuid.UUID(i) for i in (world.issue_a, world.issue_b, world.issue_nowhere)])
                )
            )
        ).all()
    )
    assert zones[uuid.UUID(world.issue_a)] == world.zone_a
    assert zones[uuid.UUID(world.issue_b)] == world.zone_b
    assert zones[uuid.UUID(world.issue_nowhere)] is None
    return world


async def _status(client: AsyncClient, issue_id: str, user: User, new_status: str = "IN_PROGRESS") -> Response:
    return await client.patch(f"/v1/issues/{issue_id}/status", json={"status": new_status}, headers=_auth(user))


async def _assign(client: AsyncClient, issue_id: str, user: User, assignee_id: uuid.UUID) -> Response:
    return await client.patch(
        f"/v1/issues/{issue_id}/assign", json={"assigned_to_id": str(assignee_id)}, headers=_auth(user)
    )


async def _staff(client: AsyncClient, issue_id: str, user: User) -> Response:
    return await client.get(f"/v1/issues/{issue_id}/assignable-staff", headers=_auth(user))


def _assert_same_as_missing(response: Response, missing: Response) -> None:
    """Out of scope must be byte-for-byte what a non-existent issue gets."""
    assert missing.status_code == 404
    assert response.status_code == 404, response.text
    assert response.content == missing.content
    assert response.json() == {"error": {"code": "NOT_FOUND", "message": "Issue not found", "details": {}}}


# ── Status changes ──────────────────────────────────────────────────────


class TestStatusScope:
    async def test_authority_can_change_an_issue_in_their_zone(self, client: AsyncClient, city: City) -> None:
        response = await _status(client, city.issue_a, city.authority_a)
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "IN_PROGRESS"

    async def test_another_zones_issue_is_the_same_404_as_a_missing_one(
        self, client: AsyncClient, db_session: AsyncSession, city: City
    ) -> None:
        missing = await _status(client, str(uuid.uuid4()), city.authority_a)
        response = await _status(client, city.issue_b, city.authority_a)
        _assert_same_as_missing(response, missing)

        issue = await db_session.get(Issue, uuid.UUID(city.issue_b))
        assert issue is not None
        await db_session.refresh(issue)
        assert issue.status == "REPORTED", "a refused change must change nothing"
        history = await db_session.scalar(
            select(func.count())
            .select_from(IssueStatusHistory)
            .where(IssueStatusHistory.issue_id == uuid.UUID(city.issue_b))
        )
        assert history == 1, "only the opening REPORTED row"

    async def test_scope_is_checked_before_the_status_machine(
        self, client: AsyncClient, city: City, db_session: AsyncSession
    ) -> None:
        """Otherwise a 400 INVALID_STATUS_TRANSITION would confirm the issue exists."""
        admin = await _user(db_session, "ADMIN")
        assert (await _status(client, city.issue_b, admin, "REJECTED")).status_code == 200

        missing = await _status(client, str(uuid.uuid4()), city.authority_a, "IN_PROGRESS")
        illegal = await _status(client, city.issue_b, city.authority_a, "IN_PROGRESS")
        _assert_same_as_missing(illegal, missing)

    async def test_an_issue_outside_every_zone_is_admin_only(
        self, client: AsyncClient, db_session: AsyncSession, city: City
    ) -> None:
        everywhere, _ = await staff_user(db_session, [city.zone_a, city.zone_b])
        missing = await _status(client, str(uuid.uuid4()), everywhere)
        _assert_same_as_missing(await _status(client, city.issue_nowhere, everywhere), missing)

        admin = await _user(db_session, "ADMIN")
        response = await _status(client, city.issue_nowhere, admin)
        assert response.status_code == 200, response.text

    async def test_admin_may_act_in_any_zone(self, client: AsyncClient, db_session: AsyncSession, city: City) -> None:
        admin = await _user(db_session, "ADMIN")
        assert (await _status(client, city.issue_a, admin)).status_code == 200
        assert (await _status(client, city.issue_b, admin)).status_code == 200

    async def test_authority_with_no_profile_has_no_jurisdiction(
        self, client: AsyncClient, db_session: AsyncSession, city: City
    ) -> None:
        """No profile means no zones — a 404, not an error that would confirm the issue."""
        bare = await _user(db_session, "AUTHORITY")
        missing = await _status(client, str(uuid.uuid4()), bare)
        _assert_same_as_missing(await _status(client, city.issue_a, bare), missing)

    async def test_department_is_not_a_scope_boundary(
        self, client: AsyncClient, db_session: AsyncSession, city: City
    ) -> None:
        """Jurisdiction is geographic: a Sanitation officer in zone A may triage a pothole there."""
        sanitation, _ = await staff_user(db_session, [city.zone_a], department_code="SAN")
        assert (await _status(client, city.issue_a, sanitation)).status_code == 200


# ── Assignment ──────────────────────────────────────────────────────────


class TestAssignScope:
    async def test_assigns_to_a_colleague_covering_the_zone(
        self, client: AsyncClient, db_session: AsyncSession, city: City
    ) -> None:
        _, colleague = await staff_user(db_session, [city.zone_a])
        response = await _assign(client, city.issue_a, city.authority_a, colleague.id)

        assert response.status_code == 200, response.text
        assert response.json()["assigned_to_id"] == str(colleague.id)

    async def test_another_zones_issue_is_the_same_404_as_a_missing_one(
        self, client: AsyncClient, db_session: AsyncSession, city: City
    ) -> None:
        """Even naming an assignee who *is* eligible for that issue — scope comes first."""
        _, b_worker = await staff_user(db_session, [city.zone_b])
        missing = await _assign(client, str(uuid.uuid4()), city.authority_a, b_worker.id)
        response = await _assign(client, city.issue_b, city.authority_a, b_worker.id)
        _assert_same_as_missing(response, missing)

        issue = await db_session.get(Issue, uuid.UUID(city.issue_b))
        await db_session.refresh(issue)
        assert issue.assigned_to_id is None

    async def test_an_issue_outside_every_zone_is_admin_only(
        self, client: AsyncClient, db_session: AsyncSession, city: City
    ) -> None:
        admin = await _user(db_session, "ADMIN")
        _, admin_profile = await staff_user(db_session, [], role="ADMIN")
        missing = await _assign(client, str(uuid.uuid4()), city.authority_a, admin_profile.id)
        _assert_same_as_missing(await _assign(client, city.issue_nowhere, city.authority_a, admin_profile.id), missing)

        response = await _assign(client, city.issue_nowhere, admin, admin_profile.id)
        assert response.status_code == 200, response.text

    async def test_assignee_from_another_zone_is_refused(
        self, client: AsyncClient, db_session: AsyncSession, city: City
    ) -> None:
        _, b_worker = await staff_user(db_session, [city.zone_b])
        response = await _assign(client, city.issue_a, city.authority_a, b_worker.id)

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "ASSIGNEE_NOT_ELIGIBLE"
        issue = await db_session.get(Issue, uuid.UUID(city.issue_a))
        await db_session.refresh(issue)
        assert issue.assigned_to_id is None

    async def test_inactive_assignee_is_refused(
        self, client: AsyncClient, db_session: AsyncSession, city: City
    ) -> None:
        _, departed = await staff_user(db_session, [city.zone_a], is_active=False)
        response = await _assign(client, city.issue_a, city.authority_a, departed.id)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "ASSIGNEE_NOT_ELIGIBLE"

    async def test_unknown_assignee_is_the_same_refusal(self, client: AsyncClient, city: City) -> None:
        """Not a 404: that would be confusable with the issue's, and would probe which ids exist."""
        response = await _assign(client, city.issue_a, city.authority_a, uuid.uuid4())
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "ASSIGNEE_NOT_ELIGIBLE"

    async def test_admin_cannot_assign_to_someone_who_cannot_see_the_issue(
        self, client: AsyncClient, db_session: AsyncSession, city: City
    ) -> None:
        """The eligibility rule binds admins too — it is about the assignee, not the actor."""
        admin = await _user(db_session, "ADMIN")
        _, b_worker = await staff_user(db_session, [city.zone_b])
        response = await _assign(client, city.issue_nowhere, admin, b_worker.id)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "ASSIGNEE_NOT_ELIGIBLE"


# ── GET /issues/{id}/assignable-staff ───────────────────────────────────


class TestAssignableStaff:
    async def test_lists_exactly_the_staff_who_can_see_the_issue(
        self, client: AsyncClient, db_session: AsyncSession, city: City
    ) -> None:
        _, a_worker = await staff_user(db_session, [city.zone_a], name="Asha Rao")
        _, both = await staff_user(db_session, [city.zone_a, city.zone_b], name="Bala Iyer")
        _, b_worker = await staff_user(db_session, [city.zone_b], name="Chitra Nair")
        _, departed = await staff_user(db_session, [city.zone_a], is_active=False, name="Dev Shah")
        _, admin_profile = await staff_user(db_session, [], role="ADMIN", name="Esha Admin")

        response = await _staff(client, city.issue_a, city.authority_a)
        assert response.status_code == 200, response.text
        listed = {item["id"] for item in response.json()["items"]}

        assert {str(a_worker.id), str(both.id), str(admin_profile.id)} <= listed
        assert str(b_worker.id) not in listed, "covers another zone only"
        assert str(departed.id) not in listed, "deactivated"

    async def test_every_listed_person_is_accepted_by_assign(
        self, client: AsyncClient, db_session: AsyncSession, city: City
    ) -> None:
        """The picker and the assign check read one rule; they must never disagree."""
        await staff_user(db_session, [city.zone_a])
        await staff_user(db_session, [city.zone_b])
        items = (await _staff(client, city.issue_a, city.authority_a)).json()["items"]
        assert items

        for item in items:
            response = await _assign(client, city.issue_a, city.authority_a, uuid.UUID(item["id"]))
            assert response.status_code == 200, f"{item['id']}: {response.text}"

    async def test_carries_only_the_minimal_fields(
        self, client: AsyncClient, db_session: AsyncSession, city: City
    ) -> None:
        await staff_user(db_session, [city.zone_a], name="Asha Rao", designation="Junior Engineer")
        items = (await _staff(client, city.issue_a, city.authority_a)).json()["items"]
        asha = next(i for i in items if i["name"] == "Asha Rao")

        assert set(asha) == {"id", "name", "designation", "department_name"}
        assert asha["designation"] == "Junior Engineer"
        assert asha["department_name"]

    async def test_is_ordered_by_name(self, client: AsyncClient, db_session: AsyncSession, city: City) -> None:
        for name in ("Zara Khan", "Arun Das", "Meera Pillai"):
            await staff_user(db_session, [city.zone_a], name=name)
        names = [i["name"] for i in (await _staff(client, city.issue_a, city.authority_a)).json()["items"]]
        assert names == sorted(names)

    async def test_another_zones_issue_is_the_same_404_as_a_missing_one(self, client: AsyncClient, city: City) -> None:
        missing = await _staff(client, str(uuid.uuid4()), city.authority_a)
        _assert_same_as_missing(await _staff(client, city.issue_b, city.authority_a), missing)

    async def test_issue_outside_every_zone_lists_only_admin_profiles(
        self, client: AsyncClient, db_session: AsyncSession, city: City
    ) -> None:
        admin = await _user(db_session, "ADMIN")
        _, admin_profile = await staff_user(db_session, [], role="ADMIN")
        _, a_worker = await staff_user(db_session, [city.zone_a])

        missing = await _staff(client, str(uuid.uuid4()), city.authority_a)
        _assert_same_as_missing(await _staff(client, city.issue_nowhere, city.authority_a), missing)

        listed = {i["id"] for i in (await _staff(client, city.issue_nowhere, admin)).json()["items"]}
        assert str(admin_profile.id) in listed
        assert str(a_worker.id) not in listed

    async def test_citizens_and_anonymous_callers_are_refused(
        self, client: AsyncClient, db_session: AsyncSession, city: City
    ) -> None:
        citizen = await _user(db_session, "CITIZEN")
        assert (await _staff(client, city.issue_a, citizen)).status_code == 403
        assert (await client.get(f"/v1/issues/{city.issue_a}/assignable-staff")).status_code == 401


# ── Public reads are untouched ──────────────────────────────────────────


class TestPublicReadsStayPublic:
    async def test_anyone_can_read_any_zones_issue(self, client: AsyncClient, city: City) -> None:
        for issue_id in (city.issue_a, city.issue_b, city.issue_nowhere):
            assert (await client.get(f"/v1/issues/{issue_id}")).status_code == 200

    async def test_list_and_nearby_are_not_zone_scoped(self, client: AsyncClient, city: City) -> None:
        listed = {i["id"] for i in (await client.get("/v1/issues", params={"page_size": 100})).json()["items"]}
        assert {city.issue_a, city.issue_b, city.issue_nowhere} <= listed

        nearby = await client.get("/v1/issues/nearby", params={"lat": B_LAT, "lng": B_LNG, "radius": 500})
        assert city.issue_b in {i["id"] for i in nearby.json()}
