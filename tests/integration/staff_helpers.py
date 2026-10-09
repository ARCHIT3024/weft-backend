"""Staff fixtures shared by the integration tests that triage issues.

Since triage became zone-scoped (`app.services.issue_access`), an AUTHORITY can
change status or assign only issues in one of their `authority_zones`. A test
that just needs *someone allowed* to move an issue — to exercise notifications,
"my reports" filters or analytics — uses `authority_for_issue` rather than a
bare AUTHORITY account, which now has no jurisdiction at all.

Not a test module (no `test_` prefix), so pytest does not collect it.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.authority_user import AuthorityUser
from app.models.authority_zone import AuthorityZone
from app.models.department import Department
from app.models.issue import Issue
from app.models.user import User

# Half-width, in degrees, of the square zone `zone_around` draws (~1 km).
_HALF_SIDE_DEG = 0.01


async def zone_around(db: AsyncSession, latitude: float, longitude: float, name: str = "Scope Zone") -> uuid.UUID:
    """A small square zone centred on the point. Raw SQL: `boundary` needs an SRID at INSERT."""
    zone_id = uuid.uuid4()
    west, east = longitude - _HALF_SIDE_DEG, longitude + _HALF_SIDE_DEG
    south, north = latitude - _HALF_SIDE_DEG, latitude + _HALF_SIDE_DEG
    await db.execute(
        text("INSERT INTO zones (id, name, boundary, is_active) VALUES (:id, :name, ST_GeomFromEWKT(:ewkt), TRUE)"),
        {
            "id": zone_id,
            "name": f"{name}-{zone_id.hex[:8]}",
            "ewkt": f"SRID=4326;POLYGON(({west} {south}, {east} {south}, {east} {north}, "
            f"{west} {north}, {west} {south}))",
        },
    )
    await db.flush()
    return zone_id


async def staff_user(
    db: AsyncSession,
    zone_ids: list[uuid.UUID],
    *,
    role: str = "AUTHORITY",
    is_active: bool = True,
    department_code: str = "PWD",
    name: str | None = None,
    designation: str | None = "Assistant Engineer",
) -> tuple[User, AuthorityUser]:
    """A staff account with an `authority_users` profile covering `zone_ids`."""
    user = User(
        email=f"{role.lower()}-{uuid.uuid4().hex[:12]}@example.com",
        name=name or f"Test {role.title()} {uuid.uuid4().hex[:6]}",
        password_hash="x",
        role=role,
        is_anonymous=False,
        is_active=is_active,
    )
    db.add(user)
    await db.flush()
    profile = await give_profile(db, user, zone_ids, department_code=department_code, designation=designation)
    return user, profile


async def give_profile(
    db: AsyncSession,
    user: User,
    zone_ids: list[uuid.UUID],
    *,
    department_code: str = "PWD",
    designation: str | None = "Assistant Engineer",
) -> AuthorityUser:
    """Attach an `authority_users` profile covering `zone_ids` to an existing account."""
    department_id = await db.scalar(select(Department.id).where(Department.code == department_code))
    profile = AuthorityUser(
        user_id=user.id,
        department_id=department_id,
        employee_id=f"EMP-{uuid.uuid4().hex[:10]}",
        designation=designation,
    )
    db.add(profile)
    await db.flush()
    db.add_all([AuthorityZone(authority_user_id=profile.id, zone_id=z) for z in zone_ids])
    await db.flush()
    return profile


async def zone_of_issue(db: AsyncSession, issue_id: str | uuid.UUID) -> uuid.UUID:
    """The issue's zone, placing it in a fresh one first if it has none.

    An issue reported where no zone was configured has `zone_id` NULL, which no
    authority covers. A zone is then drawn around its location and the issue
    placed in it — what routing would have done had the zone existed when it
    was reported.
    """
    issue = await db.get(Issue, uuid.UUID(str(issue_id)))
    assert issue is not None, f"no issue {issue_id}"
    if issue.zone_id is None:
        issue.zone_id = await zone_around(db, float(issue.latitude), float(issue.longitude))
        await db.flush()
    return issue.zone_id


async def authority_for_issue(db: AsyncSession, issue_id: str | uuid.UUID) -> User:
    """A new AUTHORITY whose zones include this issue's zone."""
    user, _ = await staff_user(db, [await zone_of_issue(db, issue_id)])
    return user


async def grant_jurisdiction(db: AsyncSession, user: User, issue_id: str | uuid.UUID) -> None:
    """Give an existing AUTHORITY account a profile covering this issue's zone."""
    await give_profile(db, user, [await zone_of_issue(db, issue_id)])
