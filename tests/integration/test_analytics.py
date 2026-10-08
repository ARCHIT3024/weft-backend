"""Integration tests for the live analytics endpoints, against a real database.

These replace `test_mock_server.py::TestAnalyticsMock`, which pinned the fixed
JSON of the Phase 0 stubs.

The fixture `world` seeds issues in known states with **explicit timestamps** —
report times, and every audit-trail row — so each figure below is a number
worked out by hand, not one read back from the code under test:

Zone A (the authority's zone)          created   history                     SLA
  a_breached    POTHOLE    REPORTED    -100h                                 PWD 72h → breached
  a_fresh       GARBAGE    IN_PROGRESS   -2h                                 SAN 24h
  a_resolved    POTHOLE    RESOLVED     -50h     resolved +10h               → 10h
  a_reresolved  WATER_LOG  RESOLVED     -60h     res +5h, reopen +20h,       → 30h (final, not 5h)
                                                 res +30h
  a_reopened    POTHOLE    IN_PROGRESS  -30h     res +4h, reopen +10h        → no resolution time
  a_rejected    OTHER      REJECTED    -500h                                 closed → not breached
  a_injection   GARBAGE    REPORTED      -1h     formula-injection payloads
  a_no_dept     OTHER      REPORTED   -1000h     no department → no SLA
Zone B (someone else's)
  b_breached    GARBAGE    REPORTED     -48h                                 SAN 24h → breached
  b_resolved    POTHOLE    RESOLVED     -20h     resolved +2h                → 2h
No zone
  unzoned       POTHOLE    REPORTED    -200h                                 PWD 72h → breached

Every test runs in one rolled-back transaction, so these are the only issues
the database holds while it runs.
"""

from __future__ import annotations

import asyncio
import csv
import io
import uuid
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import NullPool, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings
from app.core.security import create_access_token
from app.models.authority_user import AuthorityUser
from app.models.authority_zone import AuthorityZone
from app.models.department import Department
from app.models.issue import Issue
from app.models.issue_status_history import IssueStatusHistory
from app.models.user import User
from app.services import analytics_service
from app.services.issue_service import generate_issue_number

ENDPOINTS = [
    "/v1/analytics/summary",
    "/v1/analytics/heatmap",
    "/v1/analytics/resolution-times",
    "/v1/analytics/sla-breaches",
    "/v1/analytics/export",
]

INJECTION_DESCRIPTION = '=HYPERLINK("http://evil.example","click")'
INJECTION_ADDRESS = "@SUM(1+1)*cmd|' /C calc'!A0"
AWKWARD_NOTE = 'Line one, with a comma\nline two with a "quote"'


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
            "start Postgres (docker compose up -d db) to run the analytics integration tests.",
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


# ── Seeding helpers ─────────────────────────────────────────────────────


async def _zone(db: AsyncSession, ewkt: str) -> uuid.UUID:
    zone_id = uuid.uuid4()
    await db.execute(
        text("INSERT INTO zones (id, name, boundary, is_active) VALUES (:id, :name, ST_GeomFromEWKT(:ewkt), TRUE)"),
        {"id": zone_id, "name": f"Zone-{zone_id.hex[:8]}", "ewkt": ewkt},
    )
    return zone_id


async def _department(db: AsyncSession, code: str) -> uuid.UUID:
    department_id = await db.scalar(select(Department.id).where(Department.code == code))
    assert department_id is not None, f"department {code} missing — has migration 014 run?"
    return department_id


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


async def _authority(db: AsyncSession, zone_ids: list[uuid.UUID], department_code: str = "PWD") -> User:
    """An AUTHORITY account with a profile posted to a department and the given zones."""
    user = await _user(db, "AUTHORITY")
    profile = AuthorityUser(
        user_id=user.id,
        department_id=await _department(db, department_code),
        employee_id=f"EMP-{uuid.uuid4().hex[:10]}",
    )
    db.add(profile)
    await db.flush()
    for zone_id in zone_ids:
        db.add(AuthorityZone(authority_user_id=profile.id, zone_id=zone_id))
    await db.flush()
    return user


def _auth(user: User) -> dict[str, str]:
    token = create_access_token(user_id=str(user.id), role=user.role, email=user.email)
    return {"Authorization": f"Bearer {token}"}


async def _issue(
    db: AsyncSession,
    *,
    category: str,
    created_at: datetime,
    zone_id: uuid.UUID | None,
    department_id: uuid.UUID | None,
    lat: float = 12.9716,
    lng: float = 77.5946,
    transitions: Sequence[tuple[str, float]] = (),  # (new_status, hours after report)
    upvotes: int = 0,
    description: str | None = "Test issue",
    address_text: str | None = None,
    resolution_note: str | None = None,
) -> Issue:
    """Insert an issue and its full audit trail with explicit timestamps.

    The final status, and `resolved_at`, are derived from `transitions` exactly
    as `issue_service.update_status` would leave them. `upvote_count` is set
    directly: this is seed data, and the trigger's own behaviour is covered by
    test_issues.py.
    """
    status, resolved_at = "REPORTED", None
    for new_status, hours in transitions:
        if new_status == "RESOLVED":
            resolved_at = created_at + timedelta(hours=hours)
        elif status == "RESOLVED":
            resolved_at = None
        status = new_status

    issue = Issue(
        issue_number=generate_issue_number(),
        category=category,
        status=status,
        description=description,
        address_text=address_text,
        latitude=Decimal(str(lat)),
        longitude=Decimal(str(lng)),
        zone_id=zone_id,
        department_id=department_id,
        upvote_count=upvotes,
        resolved_at=resolved_at,
        resolution_note=resolution_note,
        created_at=created_at,
        updated_at=created_at,
    )
    db.add(issue)
    await db.flush()

    previous = None
    for new_status, hours in [("REPORTED", 0.0), *transitions]:
        db.add(
            IssueStatusHistory(
                issue_id=issue.id,
                previous_status=previous,
                new_status=new_status,
                created_at=created_at + timedelta(hours=hours),
            )
        )
        previous = new_status
    await db.flush()
    return issue


@dataclass
class World:
    zone_a: uuid.UUID
    zone_b: uuid.UUID
    pwd: uuid.UUID
    issues: dict[str, Issue]


@pytest.fixture
async def world(db_session: AsyncSession) -> World:
    now = datetime.now(UTC)

    def ago(hours: float) -> datetime:
        return now - timedelta(hours=hours)

    zone_a = await _zone(
        db_session, "SRID=4326;POLYGON((77.57 12.95, 77.61 12.95, 77.61 12.99, 77.57 12.99, 77.57 12.95))"
    )
    zone_b = await _zone(
        db_session, "SRID=4326;POLYGON((80.20 13.00, 80.30 13.00, 80.30 13.10, 80.20 13.10, 80.20 13.00))"
    )
    pwd, san, wsd, gen = [await _department(db_session, code) for code in ("PWD", "SAN", "WSD", "GEN")]

    a = {"zone_id": zone_a}
    b = {"zone_id": zone_b, "lat": 13.0827, "lng": 80.2707}
    issues = {
        "a_breached": await _issue(
            db_session, category="POTHOLE", created_at=ago(100), department_id=pwd, upvotes=3, **a
        ),
        "a_fresh": await _issue(
            db_session,
            category="GARBAGE_ACCUMULATION",
            created_at=ago(2),
            department_id=san,
            lat=12.9721,
            lng=77.5949,
            transitions=[("IN_PROGRESS", 1)],
            upvotes=4,
            **a,
        ),
        "a_resolved": await _issue(
            db_session,
            category="POTHOLE",
            created_at=ago(50),
            department_id=pwd,
            transitions=[("IN_PROGRESS", 3), ("RESOLVED", 10)],
            resolution_note=AWKWARD_NOTE,
            **a,
        ),
        "a_reresolved": await _issue(
            db_session,
            category="WATER_LOGGING",
            created_at=ago(60),
            department_id=wsd,
            transitions=[("RESOLVED", 5), ("IN_PROGRESS", 20), ("RESOLVED", 30)],
            **a,
        ),
        "a_reopened": await _issue(
            db_session,
            category="POTHOLE",
            created_at=ago(30),
            department_id=pwd,
            lat=12.9840,
            lng=77.6040,
            transitions=[("RESOLVED", 4), ("IN_PROGRESS", 10)],
            **a,
        ),
        "a_rejected": await _issue(
            db_session,
            category="OTHER",
            created_at=ago(500),
            department_id=gen,
            transitions=[("REJECTED", 1)],
            **a,
        ),
        "a_injection": await _issue(
            db_session,
            category="GARBAGE_ACCUMULATION",
            created_at=ago(1),
            department_id=san,
            lat=12.9600,
            lng=77.5800,
            description=INJECTION_DESCRIPTION,
            address_text=INJECTION_ADDRESS,
            **a,
        ),
        "a_no_dept": await _issue(
            db_session,
            category="OTHER",
            created_at=ago(1000),
            department_id=None,
            lat=12.9601,
            lng=77.5801,
            **a,
        ),
        "b_breached": await _issue(
            db_session, category="GARBAGE_ACCUMULATION", created_at=ago(48), department_id=san, **b
        ),
        "b_resolved": await _issue(
            db_session,
            category="POTHOLE",
            created_at=ago(20),
            department_id=pwd,
            transitions=[("RESOLVED", 2)],
            **b,
        ),
        "unzoned": await _issue(
            db_session, category="POTHOLE", created_at=ago(200), department_id=pwd, zone_id=None, lat=1.0, lng=1.0
        ),
    }
    return World(zone_a=zone_a, zone_b=zone_b, pwd=pwd, issues=issues)


async def _get(client: AsyncClient, path: str, user: User, **params) -> dict:
    response = await client.get(path, params=params, headers=_auth(user))
    assert response.status_code == 200, response.text
    return response.json()


def _parse_csv(body: bytes) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"), newline="")))


# ── Access control ──────────────────────────────────────────────────────


class TestAccess:
    @pytest.mark.parametrize("path", ENDPOINTS)
    async def test_anonymous_is_refused(self, client: AsyncClient, path: str) -> None:
        assert (await client.get(path)).status_code == 401

    @pytest.mark.parametrize("path", ENDPOINTS)
    async def test_citizen_is_refused(self, client: AsyncClient, db_session: AsyncSession, path: str) -> None:
        citizen = await _user(db_session, "CITIZEN")
        assert (await client.get(path, headers=_auth(citizen))).status_code == 403

    @pytest.mark.parametrize("path", ENDPOINTS)
    async def test_authority_without_a_profile_is_refused(
        self, client: AsyncClient, db_session: AsyncSession, path: str
    ) -> None:
        """No profile means no jurisdiction; zeros would pass for a quiet city."""
        authority = await _user(db_session, "AUTHORITY")
        response = await client.get(path, headers=_auth(authority))
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "AUTHORITY_PROFILE_MISSING"


# ── Authority scoping ───────────────────────────────────────────────────


class TestScoping:
    async def test_authority_sees_only_their_zones(
        self, client: AsyncClient, db_session: AsyncSession, world: World
    ) -> None:
        authority = await _authority(db_session, [world.zone_a])
        body = await _get(client, "/v1/analytics/summary", authority)

        assert body["total_reported"] == 8, "zone B and the unzoned issue must be invisible"
        assert body["sla_breach_count"] == 1

    async def test_authority_department_is_not_a_scope_boundary(
        self, client: AsyncClient, db_session: AsyncSession, world: World
    ) -> None:
        """A PWD supervisor still sees sanitation issues in their zone."""
        authority = await _authority(db_session, [world.zone_a], department_code="PWD")
        body = await _get(client, "/v1/analytics/summary", authority)
        garbage = next(c for c in body["category_breakdown"] if c["category"] == "GARBAGE_ACCUMULATION")
        assert garbage["count"] == 2

    async def test_other_authority_sees_only_the_other_zone(
        self, client: AsyncClient, db_session: AsyncSession, world: World
    ) -> None:
        authority = await _authority(db_session, [world.zone_b])
        body = await _get(client, "/v1/analytics/summary", authority)
        assert body["total_reported"] == 2
        assert body["total_resolved"] == 1

    async def test_filtering_by_a_zone_outside_scope_is_refused(
        self, client: AsyncClient, db_session: AsyncSession, world: World
    ) -> None:
        authority = await _authority(db_session, [world.zone_a])
        response = await client.get(
            "/v1/analytics/summary", params={"zone_id": str(world.zone_b)}, headers=_auth(authority)
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "ZONE_OUT_OF_SCOPE"

    async def test_authority_with_no_zones_sees_nothing(
        self, client: AsyncClient, db_session: AsyncSession, world: World
    ) -> None:
        authority = await _authority(db_session, [])
        body = await _get(client, "/v1/analytics/summary", authority)
        assert body["total_reported"] == 0
        assert body["avg_resolution_hours"] is None

    async def test_authority_heatmap_excludes_other_zones(
        self, client: AsyncClient, db_session: AsyncSession, world: World
    ) -> None:
        authority = await _authority(db_session, [world.zone_a])
        body = await _get(client, "/v1/analytics/heatmap", authority)
        assert all(p["lng"] < 78 for p in body["points"]), "zone B (lng 80.27) leaked into the heatmap"

    async def test_authority_sla_breaches_exclude_other_zones(
        self, client: AsyncClient, db_session: AsyncSession, world: World
    ) -> None:
        authority = await _authority(db_session, [world.zone_a])
        body = await _get(client, "/v1/analytics/sla-breaches", authority)
        assert [i["id"] for i in body["items"]] == [str(world.issues["a_breached"].id)]

    async def test_authority_export_excludes_other_zones(
        self, client: AsyncClient, db_session: AsyncSession, world: World
    ) -> None:
        authority = await _authority(db_session, [world.zone_a])
        response = await client.get("/v1/analytics/export", headers=_auth(authority))
        numbers = {row["issue_number"] for row in _parse_csv(response.content)}

        assert world.issues["a_breached"].issue_number in numbers
        assert world.issues["b_breached"].issue_number not in numbers
        assert world.issues["unzoned"].issue_number not in numbers
        assert len(numbers) == 8

    async def test_admin_sees_everything_including_unzoned(
        self, client: AsyncClient, db_session: AsyncSession, world: World
    ) -> None:
        admin = await _user(db_session, "ADMIN")
        body = await _get(client, "/v1/analytics/summary", admin)
        assert body["total_reported"] == 11
        assert body["sla_breach_count"] == 3


# ── Summary ─────────────────────────────────────────────────────────────


class TestSummary:
    async def test_status_totals(self, client: AsyncClient, db_session: AsyncSession, world: World) -> None:
        admin = await _user(db_session, "ADMIN")
        body = await _get(client, "/v1/analytics/summary", admin, zone_id=str(world.zone_a))

        assert body["total_reported"] == 8
        assert body["total_awaiting_triage"] == 3
        assert body["total_in_progress"] == 2
        assert body["total_resolved"] == 2
        assert body["total_rejected"] == 1
        assert body["total_reported"] == (
            body["total_awaiting_triage"] + body["total_in_progress"] + body["total_resolved"] + body["total_rejected"]
        )

    async def test_reopened_issues_are_counted(
        self, client: AsyncClient, db_session: AsyncSession, world: World
    ) -> None:
        admin = await _user(db_session, "ADMIN")
        body = await _get(client, "/v1/analytics/summary", admin, zone_id=str(world.zone_a))
        assert body["total_reopened"] == 2  # a_reresolved and a_reopened

    async def test_resolution_time_uses_the_resolution_that_stands(
        self, client: AsyncClient, db_session: AsyncSession, world: World
    ) -> None:
        """10h and 30h: the re-resolved issue counts to its final resolution, not its first (5h).

        Measuring to the first would give (10 + 5) / 2 = 7.5. Counting the
        still-reopened issue's abandoned 4h resolution would drag it lower still.
        """
        admin = await _user(db_session, "ADMIN")
        body = await _get(client, "/v1/analytics/summary", admin, zone_id=str(world.zone_a))
        assert body["avg_resolution_hours"] == pytest.approx(20.0, abs=0.01)
        assert body["median_resolution_hours"] == pytest.approx(20.0, abs=0.01)

    async def test_sla_breach_rules(self, client: AsyncClient, db_session: AsyncSession, world: World) -> None:
        """Only a_breached in zone A: rejected and department-less issues cannot breach."""
        admin = await _user(db_session, "ADMIN")
        body = await _get(client, "/v1/analytics/summary", admin, zone_id=str(world.zone_a))
        assert body["sla_breach_count"] == 1

    async def test_category_breakdown_lists_all_seven(
        self, client: AsyncClient, db_session: AsyncSession, world: World
    ) -> None:
        admin = await _user(db_session, "ADMIN")
        body = await _get(client, "/v1/analytics/summary", admin, zone_id=str(world.zone_a))
        counts = {c["category"]: c["count"] for c in body["category_breakdown"]}

        assert len(counts) == 7
        assert counts["POTHOLE"] == 3
        assert counts["WATER_LOGGING"] == 1
        assert counts["SEWAGE_OVERFLOW"] == 0
        assert sum(counts.values()) == body["total_reported"]

    async def test_no_resolutions_gives_null_not_zero(
        self, client: AsyncClient, db_session: AsyncSession, world: World
    ) -> None:
        """An average of 0 hours is a figure; with nothing resolved there is no figure."""
        admin = await _user(db_session, "ADMIN")
        body = await _get(
            client, "/v1/analytics/summary", admin, zone_id=str(world.zone_a), category="GARBAGE_ACCUMULATION"
        )
        assert body["total_resolved"] == 0
        assert body["avg_resolution_hours"] is None
        assert body["median_resolution_hours"] is None


# ── Filters ─────────────────────────────────────────────────────────────


class TestFilters:
    async def test_category_filter(self, client: AsyncClient, db_session: AsyncSession, world: World) -> None:
        admin = await _user(db_session, "ADMIN")
        body = await _get(client, "/v1/analytics/summary", admin, zone_id=str(world.zone_a), category="POTHOLE")
        assert body["total_reported"] == 3

    async def test_repeated_category_keys(self, client: AsyncClient, db_session: AsyncSession, world: World) -> None:
        admin = await _user(db_session, "ADMIN")
        body = await _get(
            client,
            "/v1/analytics/summary",
            admin,
            zone_id=str(world.zone_a),
            category=["POTHOLE", "WATER_LOGGING"],
        )
        assert body["total_reported"] == 4

    async def test_department_filter(self, client: AsyncClient, db_session: AsyncSession, world: World) -> None:
        admin = await _user(db_session, "ADMIN")
        body = await _get(
            client, "/v1/analytics/summary", admin, zone_id=str(world.zone_a), department_id=str(world.pwd)
        )
        assert body["total_reported"] == 3

    async def test_date_range_selects_by_report_time(
        self, client: AsyncClient, db_session: AsyncSession, world: World
    ) -> None:
        """Reported within the last 55h in zone A: a_fresh, a_resolved, a_reopened, a_injection."""
        admin = await _user(db_session, "ADMIN")
        since = (datetime.now(UTC) - timedelta(hours=55)).isoformat()
        body = await _get(client, "/v1/analytics/summary", admin, zone_id=str(world.zone_a), from_date=since)
        assert body["total_reported"] == 4

    async def test_to_date_excludes_later_reports(
        self, client: AsyncClient, db_session: AsyncSession, world: World
    ) -> None:
        """Reported more than 55h ago in zone A: a_breached, a_reresolved, a_rejected, a_no_dept."""
        admin = await _user(db_session, "ADMIN")
        until = (datetime.now(UTC) - timedelta(hours=55)).isoformat()
        body = await _get(client, "/v1/analytics/summary", admin, zone_id=str(world.zone_a), to_date=until)
        assert body["total_reported"] == 4

    async def test_inverted_date_range_is_rejected(self, client: AsyncClient, db_session: AsyncSession) -> None:
        admin = await _user(db_session, "ADMIN")
        response = await client.get(
            "/v1/analytics/summary",
            params={"from_date": "2026-09-30T00:00:00Z", "to_date": "2026-09-01T00:00:00Z"},
            headers=_auth(admin),
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "INVALID_DATE_RANGE"

    async def test_unknown_category_is_a_validation_error(self, client: AsyncClient, db_session: AsyncSession) -> None:
        admin = await _user(db_session, "ADMIN")
        response = await client.get("/v1/analytics/summary", params={"category": "NOPE"}, headers=_auth(admin))
        assert response.status_code == 422


# ── Heatmap ─────────────────────────────────────────────────────────────


class TestHeatmap:
    async def test_open_issues_snap_to_the_grid(
        self, client: AsyncClient, db_session: AsyncSession, world: World
    ) -> None:
        """Five open issues in zone A fall in three 0.01° cells, densest first."""
        admin = await _user(db_session, "ADMIN")
        body = await _get(client, "/v1/analytics/heatmap", admin, zone_id=str(world.zone_a))

        assert body["grid_size_deg"] == 0.01
        assert body["truncated"] is False
        cells = [(p["lat"], p["lng"], p["issue_count"], p["total_upvotes"]) for p in body["points"]]
        assert cells == [
            (12.96, 77.58, 2, 0),  # a_injection, a_no_dept
            (12.97, 77.59, 2, 7),  # a_breached (3), a_fresh (4)
            (12.98, 77.60, 1, 0),  # a_reopened
        ]

    async def test_status_parameter_overrides_the_open_default(
        self, client: AsyncClient, db_session: AsyncSession, world: World
    ) -> None:
        admin = await _user(db_session, "ADMIN")
        body = await _get(client, "/v1/analytics/heatmap", admin, zone_id=str(world.zone_a), status="RESOLVED")
        assert sum(p["issue_count"] for p in body["points"]) == 2

    async def test_coarser_grid_merges_cells(self, client: AsyncClient, db_session: AsyncSession, world: World) -> None:
        admin = await _user(db_session, "ADMIN")
        body = await _get(client, "/v1/analytics/heatmap", admin, zone_id=str(world.zone_a), grid=1.0)
        assert [(p["lat"], p["lng"], p["issue_count"]) for p in body["points"]] == [(13.0, 78.0, 5)]

    async def test_grid_size_is_bounded(self, client: AsyncClient, db_session: AsyncSession) -> None:
        admin = await _user(db_session, "ADMIN")
        response = await client.get("/v1/analytics/heatmap", params={"grid": 0.00001}, headers=_auth(admin))
        assert response.status_code == 422

    async def test_point_count_is_capped(
        self, client: AsyncClient, db_session: AsyncSession, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(analytics_service, "MAX_HEATMAP_POINTS", 2)
        admin = await _user(db_session, "ADMIN")
        body = await _get(client, "/v1/analytics/heatmap", admin, zone_id=str(world.zone_a))
        assert len(body["points"]) == 2
        assert body["truncated"] is True


# ── Resolution times ────────────────────────────────────────────────────


class TestResolutionTimes:
    async def test_grouped_by_category(self, client: AsyncClient, db_session: AsyncSession, world: World) -> None:
        admin = await _user(db_session, "ADMIN")
        body = await _get(client, "/v1/analytics/resolution-times", admin, zone_id=str(world.zone_a))

        assert body["group_by"] == "category"
        assert body["overall"]["resolved_count"] == 2
        assert body["overall"]["avg_hours"] == pytest.approx(20.0, abs=0.01)
        groups = {g["key"]: g for g in body["groups"]}
        assert set(groups) == {"POTHOLE", "WATER_LOGGING"}
        assert groups["POTHOLE"]["avg_hours"] == pytest.approx(10.0, abs=0.01)
        assert groups["WATER_LOGGING"]["avg_hours"] == pytest.approx(30.0, abs=0.01)
        assert groups["WATER_LOGGING"]["label"] == "WATER_LOGGING"

    async def test_grouped_by_department_carries_names(
        self, client: AsyncClient, db_session: AsyncSession, world: World
    ) -> None:
        admin = await _user(db_session, "ADMIN")
        body = await _get(
            client, "/v1/analytics/resolution-times", admin, zone_id=str(world.zone_a), group_by="department"
        )
        labels = {g["label"]: g["median_hours"] for g in body["groups"]}
        assert labels == {
            "Public Works Department": pytest.approx(10.0, abs=0.01),
            "Water & Sewerage Department": pytest.approx(30.0, abs=0.01),
        }
        assert str(world.pwd) in {g["key"] for g in body["groups"]}

    async def test_grouped_by_zone_labels_unzoned_issues(
        self, client: AsyncClient, db_session: AsyncSession, world: World
    ) -> None:
        admin = await _user(db_session, "ADMIN")
        body = await _get(client, "/v1/analytics/resolution-times", admin, group_by="zone")
        assert {g["key"] for g in body["groups"]} == {str(world.zone_a), str(world.zone_b)}
        assert body["overall"]["resolved_count"] == 3

    async def test_trend_buckets_cover_every_resolution(
        self, client: AsyncClient, db_session: AsyncSession, world: World
    ) -> None:
        admin = await _user(db_session, "ADMIN")
        body = await _get(client, "/v1/analytics/resolution-times", admin, interval="day")

        periods = [p["period_start"] for p in body["trend"]]
        assert periods == sorted(periods), "trend must read oldest first"
        assert sum(p["resolved_count"] for p in body["trend"]) == 3
        assert body["trend_truncated"] is False

    async def test_empty_scope_has_no_figures(
        self, client: AsyncClient, db_session: AsyncSession, world: World
    ) -> None:
        authority = await _authority(db_session, [])
        body = await _get(client, "/v1/analytics/resolution-times", authority)
        assert body["overall"] == {"resolved_count": 0, "avg_hours": None, "median_hours": None}
        assert body["groups"] == []
        assert body["trend"] == []

    async def test_unknown_group_by_is_rejected(self, client: AsyncClient, db_session: AsyncSession) -> None:
        admin = await _user(db_session, "ADMIN")
        response = await client.get(
            "/v1/analytics/resolution-times", params={"group_by": "reporter"}, headers=_auth(admin)
        )
        assert response.status_code == 422

    async def test_real_status_machine_feeds_the_figures(self, client: AsyncClient, db_session: AsyncSession) -> None:
        """End to end through PATCH /status: resolve, reopen, resolve again.

        The other tests seed history directly to control the clock; this one
        proves the figures read what the real status handler actually writes.
        """
        zone_id = await _zone(
            db_session, "SRID=4326;POLYGON((77.57 12.95, 77.61 12.95, 77.61 12.99, 77.57 12.99, 77.57 12.95))"
        )
        submitted = await client.post(
            "/v1/issues", data={"category": "POTHOLE", "latitude": "12.9716", "longitude": "77.5946"}
        )
        issue_id = submitted.json()["issue_id"]
        triage = _auth(await _user(db_session, "AUTHORITY"))
        for target in ("RESOLVED", "IN_PROGRESS", "RESOLVED"):
            moved = await client.patch(f"/v1/issues/{issue_id}/status", json={"status": target}, headers=triage)
            assert moved.status_code == 200, moved.text

        admin = await _user(db_session, "ADMIN")
        times = await _get(client, "/v1/analytics/resolution-times", admin, zone_id=str(zone_id))
        summary = await _get(client, "/v1/analytics/summary", admin, zone_id=str(zone_id))

        assert times["overall"]["resolved_count"] == 1
        assert times["overall"]["avg_hours"] >= 0
        assert summary["total_reopened"] == 1


# ── SLA breaches ────────────────────────────────────────────────────────


class TestSlaBreaches:
    async def test_most_overdue_first(self, client: AsyncClient, db_session: AsyncSession, world: World) -> None:
        """unzoned 200-72=128h, a_breached 100-72=28h, b_breached 48-24=24h."""
        admin = await _user(db_session, "ADMIN")
        body = await _get(client, "/v1/analytics/sla-breaches", admin)

        assert body["total"] == 3
        assert [i["id"] for i in body["items"]] == [
            str(world.issues["unzoned"].id),
            str(world.issues["a_breached"].id),
            str(world.issues["b_breached"].id),
        ]
        first = body["items"][0]
        assert first["sla_hours"] == 72
        assert first["hours_overdue"] == pytest.approx(128, abs=0.5)
        assert first["age_hours"] == pytest.approx(200, abs=0.5)
        assert first["department_name"] == "Public Works Department"
        assert first["zone_id"] is None

    async def test_uses_each_departments_own_sla(
        self, client: AsyncClient, db_session: AsyncSession, world: World
    ) -> None:
        """b_breached is 48h old: inside PWD's 72h but past Sanitation's 24h."""
        admin = await _user(db_session, "ADMIN")
        body = await _get(client, "/v1/analytics/sla-breaches", admin, zone_id=str(world.zone_b))
        assert body["total"] == 1
        assert body["items"][0]["sla_hours"] == 24
        assert body["items"][0]["zone_name"] is not None

    async def test_pagination(self, client: AsyncClient, db_session: AsyncSession, world: World) -> None:
        admin = await _user(db_session, "ADMIN")
        body = await _get(client, "/v1/analytics/sla-breaches", admin, page=2, page_size=2)
        assert body["total"] == 3
        assert body["total_pages"] == 2
        assert [i["id"] for i in body["items"]] == [str(world.issues["b_breached"].id)]

    async def test_page_size_is_capped(self, client: AsyncClient, db_session: AsyncSession) -> None:
        admin = await _user(db_session, "ADMIN")
        response = await client.get("/v1/analytics/sla-breaches", params={"page_size": 101}, headers=_auth(admin))
        assert response.status_code == 422


# ── CSV export ──────────────────────────────────────────────────────────


class TestExport:
    async def _export(self, client: AsyncClient, user: User, **params) -> list[dict[str, str]]:
        response = await client.get("/v1/analytics/export", params=params, headers=_auth(user))
        assert response.status_code == 200, response.text
        return _parse_csv(response.content)

    async def test_is_a_csv_download(self, client: AsyncClient, db_session: AsyncSession, world: World) -> None:
        admin = await _user(db_session, "ADMIN")
        response = await client.get("/v1/analytics/export", params={"format": "csv"}, headers=_auth(admin))

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        assert response.headers["content-disposition"].startswith('attachment; filename="weft-issues-')
        assert response.content.startswith(b"\xef\xbb\xbf"), "Excel needs the BOM to read UTF-8"
        assert b"\r\n" in response.content

    async def test_one_row_per_issue_with_every_column(
        self, client: AsyncClient, db_session: AsyncSession, world: World
    ) -> None:
        admin = await _user(db_session, "ADMIN")
        rows = await self._export(client, admin)

        assert len(rows) == 11
        assert tuple(rows[0]) == analytics_service.EXPORT_COLUMNS
        assert "reporter_id" not in rows[0], "the export must not undo anonymous reporting"

    async def test_citizen_text_cannot_become_a_formula(
        self, client: AsyncClient, db_session: AsyncSession, world: World
    ) -> None:
        admin = await _user(db_session, "ADMIN")
        rows = await self._export(client, admin)
        row = next(r for r in rows if r["issue_number"] == world.issues["a_injection"].issue_number)

        assert row["description"] == "'" + INJECTION_DESCRIPTION
        assert row["address_text"] == "'" + INJECTION_ADDRESS
        for r in rows:
            for column, value in r.items():
                assert not value.startswith(("=", "+", "@", "\t", "\r")), f"{column} is executable: {value!r}"

    async def test_awkward_text_round_trips(self, client: AsyncClient, db_session: AsyncSession, world: World) -> None:
        """Commas, quotes and newlines in free text must not break the row structure."""
        admin = await _user(db_session, "ADMIN")
        rows = await self._export(client, admin)
        row = next(r for r in rows if r["issue_number"] == world.issues["a_resolved"].issue_number)
        assert row["resolution_note"] == AWKWARD_NOTE

    async def test_derived_columns(self, client: AsyncClient, db_session: AsyncSession, world: World) -> None:
        admin = await _user(db_session, "ADMIN")
        rows = {r["issue_number"]: r for r in await self._export(client, admin)}

        reresolved = rows[world.issues["a_reresolved"].issue_number]
        assert reresolved["status"] == "RESOLVED"
        assert reresolved["resolution_hours"] == "30.00"
        assert reresolved["reopen_count"] == "1"

        reopened = rows[world.issues["a_reopened"].issue_number]
        assert reopened["resolved_at"] == ""
        assert reopened["resolution_hours"] == ""

        breached = rows[world.issues["a_breached"].issue_number]
        assert breached["sla_breached"] == "true"
        assert breached["sla_hours"] == "72"
        assert breached["latitude"] == "12.9716000"

    async def test_filters_apply(self, client: AsyncClient, db_session: AsyncSession, world: World) -> None:
        admin = await _user(db_session, "ADMIN")
        rows = await self._export(client, admin, zone_id=str(world.zone_b))
        assert {r["issue_number"] for r in rows} == {
            world.issues["b_breached"].issue_number,
            world.issues["b_resolved"].issue_number,
        }

    async def test_empty_export_still_has_a_header(
        self, client: AsyncClient, db_session: AsyncSession, world: World
    ) -> None:
        authority = await _authority(db_session, [])
        response = await client.get("/v1/analytics/export", headers=_auth(authority))
        assert response.content.decode("utf-8-sig").splitlines() == [",".join(analytics_service.EXPORT_COLUMNS)]

    async def test_oversized_export_is_refused_not_truncated(
        self, client: AsyncClient, db_session: AsyncSession, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(analytics_service, "EXPORT_MAX_ROWS", 5)
        admin = await _user(db_session, "ADMIN")
        response = await client.get("/v1/analytics/export", headers=_auth(admin))
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "EXPORT_TOO_LARGE"

    async def test_pdf_is_not_faked(self, client: AsyncClient, db_session: AsyncSession) -> None:
        """PDF is task 4.18. Until then: a clear 501, never a placeholder file."""
        admin = await _user(db_session, "ADMIN")
        response = await client.get("/v1/analytics/export", params={"format": "pdf"}, headers=_auth(admin))
        assert response.status_code == 501
        assert response.json()["error"]["code"] == "EXPORT_FORMAT_NOT_SUPPORTED"

    async def test_unknown_format_is_a_validation_error(self, client: AsyncClient, db_session: AsyncSession) -> None:
        admin = await _user(db_session, "ADMIN")
        response = await client.get("/v1/analytics/export", params={"format": "xlsx"}, headers=_auth(admin))
        assert response.status_code == 422
