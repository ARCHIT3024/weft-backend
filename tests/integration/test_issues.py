"""Integration tests for the real issue lifecycle, against a live database.

These replace `test_mock_server.py::TestIssuesMock` / `TestUpvotesMock`, which
pinned static JSON from the Phase 0 stubs.

What is actually being proven here — each of these is a property that unit tests
with a faked session could not establish, because the behaviour lives in
PostgreSQL rather than in Python:

* **Anonymous submission works** and leaves `reporter_id` NULL.
* **Routing happens at write time** — `ST_Contains` picks the zone, and the
  category lookup picks the department.
* **`issues.upvote_count` is moved by the `trg_upvote_count` trigger**, not by
  application code. The tests assert the counter without anything ever assigning
  to it.
* **The composite primary key is what stops double-voting**, not a prior SELECT.
* **The status machine refuses illegal moves** and records every legal one.

Module skips cleanly when no database is reachable, matching `test_auth.py`.
"""

from __future__ import annotations

import asyncio
import io
import uuid
from collections.abc import AsyncGenerator

import pytest
from httpx import AsyncClient
from sqlalchemy import NullPool, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings
from app.core.security import create_access_token
from app.models.department import Department
from app.models.issue import Issue
from app.models.user import User
from tests.integration.staff_helpers import authority_for_issue

# Inside "Central Zone" as created by `_zone` below.
LAT, LNG = 12.9716, 77.5946
# Far outside it — used to prove the unassigned path is real.
FAR_LAT, FAR_LNG = 1.0, 1.0


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
            "start Postgres (docker compose up -d db) to run the issue integration tests.",
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


async def _zone(db: AsyncSession, name: str = "Central Zone") -> uuid.UUID:
    """A rectangle around central Bengaluru containing (LAT, LNG).

    Written with raw SQL because `zones.boundary` is NOT NULL and needs the
    geometry present at INSERT with an explicit SRID.
    """
    zone_id = uuid.uuid4()
    await db.execute(
        text(
            """
            INSERT INTO zones (id, name, boundary, is_active)
            VALUES (:id, :name, ST_GeomFromEWKT(:ewkt), TRUE)
            """
        ),
        {
            "id": zone_id,
            "name": f"{name}-{zone_id.hex[:8]}",
            "ewkt": "SRID=4326;POLYGON((77.5700 12.9500, 77.6100 12.9500, "
            "77.6100 12.9900, 77.5700 12.9900, 77.5700 12.9500))",
        },
    )
    await db.flush()
    return zone_id


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


async def _submit(client: AsyncClient, **overrides) -> dict:
    form = {
        "category": "POTHOLE",
        "latitude": str(LAT),
        "longitude": str(LNG),
        "description": "Large pothole near the junction",
        **overrides,
    }
    response = await client.post("/v1/issues", data=form)
    assert response.status_code == 201, response.text
    return response.json()


# ── Submission ──────────────────────────────────────────────────────────


class TestSubmission:
    async def test_anonymous_submission_succeeds(self, client: AsyncClient, db_session: AsyncSession) -> None:
        """Anonymous reporting is a product requirement, not an edge case."""
        await _zone(db_session)
        body = await _submit(client)

        assert body["status"] == "REPORTED"
        assert body["issue_number"].startswith("ISS-")

        issue = await db_session.get(Issue, uuid.UUID(body["issue_id"]))
        assert issue is not None
        assert issue.reporter_id is None, "anonymous submission must not attach a reporter"

    async def test_submission_assigns_zone_by_point_in_polygon(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        zone_id = await _zone(db_session)
        body = await _submit(client)
        assert body["zone_id"] == str(zone_id)

    async def test_point_outside_every_zone_gets_no_zone(self, client: AsyncClient, db_session: AsyncSession) -> None:
        """The unassigned path is real, not theoretical — it must not error."""
        await _zone(db_session)
        body = await _submit(client, latitude=str(FAR_LAT), longitude=str(FAR_LNG))
        assert body["zone_id"] is None

    async def test_submission_routes_category_to_department(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        """POTHOLE routes to Public Works, per migration 014's seeded mapping."""
        await _zone(db_session)
        body = await _submit(client, category="POTHOLE")

        assert body["department_id"] is not None
        department = await db_session.get(Department, uuid.UUID(body["department_id"]))
        assert department is not None
        assert department.code == "PWD"

    async def test_garbage_routes_to_sanitation(self, client: AsyncClient, db_session: AsyncSession) -> None:
        body = await _submit(client, category="GARBAGE_ACCUMULATION")
        department = await db_session.get(Department, uuid.UUID(body["department_id"]))
        assert department is not None
        assert department.code == "SAN"

    async def test_authenticated_submission_records_the_reporter(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        user = await _user(db_session)
        response = await client.post(
            "/v1/issues",
            data={"category": "POTHOLE", "latitude": str(LAT), "longitude": str(LNG)},
            headers=_auth(user),
        )
        assert response.status_code == 201, response.text

        issue = await db_session.get(Issue, uuid.UUID(response.json()["issue_id"]))
        assert issue is not None
        assert issue.reporter_id == user.id

    async def test_submission_opens_the_audit_trail(self, client: AsyncClient) -> None:
        """Every issue must have a history row from the moment it exists."""
        body = await _submit(client)
        detail = await client.get(f"/v1/issues/{body['issue_id']}")
        history = detail.json()["status_history"]

        assert len(history) == 1
        assert history[0]["previous_status"] is None, "the opening row is the only one with no predecessor"
        assert history[0]["new_status"] == "REPORTED"

    async def test_invalid_category_is_rejected(self, client: AsyncClient) -> None:
        response = await client.post(
            "/v1/issues",
            data={"category": "NOT_A_CATEGORY", "latitude": str(LAT), "longitude": str(LNG)},
        )
        assert response.status_code == 422

    async def test_out_of_range_latitude_is_rejected(self, client: AsyncClient) -> None:
        response = await client.post(
            "/v1/issues",
            data={"category": "POTHOLE", "latitude": "91.0", "longitude": str(LNG)},
        )
        assert response.status_code == 422


# ── Retrieval ───────────────────────────────────────────────────────────


class TestRetrieval:
    async def test_list_returns_the_submitted_issue(self, client: AsyncClient) -> None:
        body = await _submit(client)
        response = await client.get("/v1/issues", params={"page_size": 100})

        assert response.status_code == 200
        ids = [item["id"] for item in response.json()["items"]]
        assert body["issue_id"] in ids

    async def test_list_filters_by_category(self, client: AsyncClient) -> None:
        await _submit(client, category="POTHOLE")
        pothole = await _submit(client, category="WATER_LOGGING")

        response = await client.get("/v1/issues", params={"category": "WATER_LOGGING", "page_size": 100})
        items = response.json()["items"]

        assert pothole["issue_id"] in [i["id"] for i in items]
        assert all(i["category"] == "WATER_LOGGING" for i in items)

    async def test_list_filters_by_status(self, client: AsyncClient) -> None:
        await _submit(client)
        response = await client.get("/v1/issues", params={"status": "RESOLVED", "page_size": 100})
        assert all(i["status"] == "RESOLVED" for i in response.json()["items"])

    async def test_detail_404s_for_an_unknown_id(self, client: AsyncClient) -> None:
        response = await client.get(f"/v1/issues/{uuid.uuid4()}")
        assert response.status_code == 404

    async def test_nearby_returns_a_real_distance(self, client: AsyncClient) -> None:
        """Distance must be metres on the spheroid, not degrees dressed up."""
        body = await _submit(client)
        response = await client.get("/v1/issues/nearby", params={"lat": LAT, "lng": LNG, "radius": 1000})

        assert response.status_code == 200
        match = next((i for i in response.json() if i["id"] == body["issue_id"]), None)
        assert match is not None
        assert match["distance_m"] < 50, "an issue at the query point is metres away, not degrees"

    async def test_nearby_excludes_issues_outside_the_radius(self, client: AsyncClient) -> None:
        far = await _submit(client, latitude=str(FAR_LAT), longitude=str(FAR_LNG))
        response = await client.get("/v1/issues/nearby", params={"lat": LAT, "lng": LNG, "radius": 1000})
        assert far["issue_id"] not in [i["id"] for i in response.json()]

    async def test_nearby_route_is_not_shadowed_by_the_id_route(self, client: AsyncClient) -> None:
        """`/issues/nearby` must not be parsed as `/issues/{uuid}`."""
        response = await client.get("/v1/issues/nearby", params={"lat": LAT, "lng": LNG})
        assert response.status_code == 200


# ── Upvotes ─────────────────────────────────────────────────────────────


class TestUpvotes:
    async def test_upvote_moves_the_counter_via_the_trigger(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        """Nothing in Python writes upvote_count — the database trigger does."""
        body = await _submit(client)
        user = await _user(db_session)

        response = await client.post(f"/v1/issues/{body['issue_id']}/upvote", headers=_auth(user))
        assert response.status_code == 201, response.text
        assert response.json()["upvote_count"] == 1

        count = await db_session.scalar(select(Issue.upvote_count).where(Issue.id == uuid.UUID(body["issue_id"])))
        assert count == 1

    async def test_duplicate_upvote_is_rejected_by_the_primary_key(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        body = await _submit(client)
        user = await _user(db_session)
        headers = _auth(user)

        assert (await client.post(f"/v1/issues/{body['issue_id']}/upvote", headers=headers)).status_code == 201
        second = await client.post(f"/v1/issues/{body['issue_id']}/upvote", headers=headers)

        assert second.status_code == 409
        assert second.json()["error"]["code"] == "ALREADY_UPVOTED"

    async def test_withdrawing_an_upvote_decrements_the_counter(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        body = await _submit(client)
        user = await _user(db_session)
        headers = _auth(user)

        await client.post(f"/v1/issues/{body['issue_id']}/upvote", headers=headers)
        response = await client.delete(f"/v1/issues/{body['issue_id']}/upvote", headers=headers)
        assert response.status_code == 204

        count = await db_session.scalar(select(Issue.upvote_count).where(Issue.id == uuid.UUID(body["issue_id"])))
        assert count == 0

    async def test_withdrawing_an_upvote_that_was_never_cast_is_idempotent(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        """A 404 here would leak whether a given user had upvoted."""
        body = await _submit(client)
        user = await _user(db_session)
        response = await client.delete(f"/v1/issues/{body['issue_id']}/upvote", headers=_auth(user))
        assert response.status_code == 204

    async def test_anonymous_users_cannot_upvote(self, client: AsyncClient) -> None:
        body = await _submit(client)
        response = await client.post(f"/v1/issues/{body['issue_id']}/upvote")
        assert response.status_code == 401

    async def test_two_users_upvoting_gives_a_count_of_two(self, client: AsyncClient, db_session: AsyncSession) -> None:
        body = await _submit(client)
        first, second = await _user(db_session), await _user(db_session)

        await client.post(f"/v1/issues/{body['issue_id']}/upvote", headers=_auth(first))
        response = await client.post(f"/v1/issues/{body['issue_id']}/upvote", headers=_auth(second))
        assert response.json()["upvote_count"] == 2


# ── Triage ──────────────────────────────────────────────────────────────


class TestStatusTransitions:
    async def test_authority_can_start_work(self, client: AsyncClient, db_session: AsyncSession) -> None:
        body = await _submit(client)
        authority = await authority_for_issue(db_session, body["issue_id"])

        response = await client.patch(
            f"/v1/issues/{body['issue_id']}/status",
            json={"status": "IN_PROGRESS", "note": "Crew dispatched"},
            headers=_auth(authority),
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "IN_PROGRESS"

    async def test_transition_is_recorded_in_history(self, client: AsyncClient, db_session: AsyncSession) -> None:
        body = await _submit(client)
        authority = await authority_for_issue(db_session, body["issue_id"])

        await client.patch(
            f"/v1/issues/{body['issue_id']}/status",
            json={"status": "IN_PROGRESS", "note": "Crew dispatched"},
            headers=_auth(authority),
        )
        detail = (await client.get(f"/v1/issues/{body['issue_id']}")).json()
        newest = detail["status_history"][0]

        assert len(detail["status_history"]) == 2
        assert newest["previous_status"] == "REPORTED"
        assert newest["new_status"] == "IN_PROGRESS"
        assert newest["changed_by_id"] == str(authority.id)
        assert newest["note"] == "Crew dispatched"

    async def test_resolving_sets_resolved_at(self, client: AsyncClient, db_session: AsyncSession) -> None:
        body = await _submit(client)
        authority = await authority_for_issue(db_session, body["issue_id"])

        response = await client.patch(
            f"/v1/issues/{body['issue_id']}/status",
            json={"status": "RESOLVED"},
            headers=_auth(authority),
        )
        assert response.json()["resolved_at"] is not None

    async def test_reopening_clears_resolved_at(self, client: AsyncClient, db_session: AsyncSession) -> None:
        """A stale resolved_at would corrupt resolution-time analytics."""
        body = await _submit(client)
        authority = await authority_for_issue(db_session, body["issue_id"])
        headers = _auth(authority)

        await client.patch(f"/v1/issues/{body['issue_id']}/status", json={"status": "RESOLVED"}, headers=headers)
        response = await client.patch(
            f"/v1/issues/{body['issue_id']}/status",
            json={"status": "IN_PROGRESS", "note": "Fix did not hold"},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["resolved_at"] is None

    async def test_rejected_is_terminal(self, client: AsyncClient, db_session: AsyncSession) -> None:
        body = await _submit(client)
        authority = await authority_for_issue(db_session, body["issue_id"])
        headers = _auth(authority)

        await client.patch(f"/v1/issues/{body['issue_id']}/status", json={"status": "REJECTED"}, headers=headers)
        response = await client.patch(
            f"/v1/issues/{body['issue_id']}/status", json={"status": "IN_PROGRESS"}, headers=headers
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "INVALID_STATUS_TRANSITION"

    async def test_resolved_cannot_jump_back_to_reported(self, client: AsyncClient, db_session: AsyncSession) -> None:
        body = await _submit(client)
        authority = await authority_for_issue(db_session, body["issue_id"])
        headers = _auth(authority)

        await client.patch(f"/v1/issues/{body['issue_id']}/status", json={"status": "RESOLVED"}, headers=headers)
        response = await client.patch(
            f"/v1/issues/{body['issue_id']}/status", json={"status": "REPORTED"}, headers=headers
        )
        assert response.status_code == 400

    async def test_transition_to_the_current_status_is_refused(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        """Otherwise the audit trail fills with rows recording nothing."""
        body = await _submit(client)
        authority = await authority_for_issue(db_session, body["issue_id"])
        response = await client.patch(
            f"/v1/issues/{body['issue_id']}/status",
            json={"status": "REPORTED"},
            headers=_auth(authority),
        )
        assert response.status_code == 400

    async def test_citizen_cannot_change_status(self, client: AsyncClient, db_session: AsyncSession) -> None:
        body = await _submit(client)
        citizen = await _user(db_session, role="CITIZEN")
        response = await client.patch(
            f"/v1/issues/{body['issue_id']}/status",
            json={"status": "RESOLVED"},
            headers=_auth(citizen),
        )
        assert response.status_code == 403

    async def test_anonymous_cannot_change_status(self, client: AsyncClient) -> None:
        body = await _submit(client)
        response = await client.patch(f"/v1/issues/{body['issue_id']}/status", json={"status": "RESOLVED"})
        assert response.status_code == 401


# ── Image upload ────────────────────────────────────────────────────────


def _jpeg(width: int = 2400, height: int = 1600) -> bytes:
    """A real JPEG, large enough that the resize step must do something."""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (200, 90, 40)).save(buffer, "JPEG")
    return buffer.getvalue()


class TestImageUpload:
    """Regression cover for the multipart path.

    This class exists because a real bug shipped past 392 passing tests: the
    `images` parameter was declared `list[UploadFile] | None`, and under that
    union FastAPI does not collect the repeated multipart field into a list, so
    **every** submission carrying a photo failed with a 422. Nothing caught it
    because no test posted an actual file — the form-only tests above all pass
    against the broken signature. These tests post real bytes.
    """

    async def test_submission_with_an_image_succeeds(self, client: AsyncClient) -> None:
        response = await client.post(
            "/v1/issues",
            data={"category": "POTHOLE", "latitude": str(LAT), "longitude": str(LNG)},
            files=[("images", ("pothole.jpg", _jpeg(), "image/jpeg"))],
        )
        assert response.status_code == 201, response.text

        body = response.json()
        assert len(body["images"]) == 1
        assert body["image_url"] is not None
        assert body["image_url"].endswith(".jpg")

    async def test_multiple_images_are_all_stored(self, client: AsyncClient) -> None:
        response = await client.post(
            "/v1/issues",
            data={"category": "POTHOLE", "latitude": str(LAT), "longitude": str(LNG)},
            files=[
                ("images", ("a.jpg", _jpeg(600, 400), "image/jpeg")),
                ("images", ("b.jpg", _jpeg(600, 400), "image/jpeg")),
            ],
        )
        assert response.status_code == 201, response.text
        assert len(response.json()["images"]) == 2

    async def test_too_many_images_is_rejected(self, client: AsyncClient) -> None:
        from app.config import settings

        files = [
            ("images", (f"{i}.jpg", _jpeg(320, 240), "image/jpeg")) for i in range(settings.MAX_IMAGES_PER_ISSUE + 1)
        ]
        response = await client.post(
            "/v1/issues",
            data={"category": "POTHOLE", "latitude": str(LAT), "longitude": str(LNG)},
            files=files,
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "TOO_MANY_IMAGES"

    async def test_an_unreadable_file_does_not_lose_the_report(self, client: AsyncClient) -> None:
        """A hazard reported with a corrupt photo is still a reported hazard."""
        response = await client.post(
            "/v1/issues",
            data={"category": "POTHOLE", "latitude": str(LAT), "longitude": str(LNG)},
            files=[("images", ("not-really.jpg", b"this is not an image", "image/jpeg"))],
        )
        assert response.status_code == 201, response.text
        assert response.json()["images"] == []
        assert response.json()["image_url"] is None

    async def test_submission_without_images_still_works(self, client: AsyncClient) -> None:
        """The empty-list default must not become a required field."""
        response = await client.post(
            "/v1/issues",
            data={"category": "POTHOLE", "latitude": str(LAT), "longitude": str(LNG)},
        )
        assert response.status_code == 201, response.text
        assert response.json()["images"] == []
