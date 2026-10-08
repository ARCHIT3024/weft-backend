"""Admin business logic — authority accounts, departments, zones, system stats.

The router stays thin: it checks the ADMIN role, translates HTTP into these
calls and back. Everything that touches the database lives here.

Four properties this module exists to guarantee:

* **Uniqueness is settled by the database, not by a prior SELECT.** Email,
  employee ID and department code each have a unique constraint; inserts run
  inside a SAVEPOINT and a violation is mapped to the right 409 by constraint
  name. Two concurrent requests both passing a "does it exist?" check is
  exactly the race a pre-check cannot close. Department *name* has no
  constraint (migration 003), so it is serialised with a transaction-scoped
  advisory lock instead — see `create_department`.
* **Deactivation ends every session in the same transaction.** `users.is_active`
  flips and every live refresh token for the account is revoked together, so
  there is no window in which the account is off but a stored refresh token
  still rotates. `get_current_user` already refuses the access token.
* **An admin cannot lock themselves out.** Self-deactivation is refused before
  anything is touched.
* **A zone boundary is never stored unless PostGIS calls it valid.** Shape and
  range checks run in Python first; `ST_IsValid` has the last word, so a
  self-intersecting polygon is a 400 with PostGIS's own reason, not a 500 and
  not a silently broken `ST_Contains` lookup on every later submission.

**Initial passwords are admin-supplied.** TRD §8 describes a system-generated
temporary password delivered by AWS SES. There is no email infrastructure, and
without it a generated password could only be handed back in this API's
response body — a credential in a response, logs and browser history. The admin
therefore sets the initial password, under the same policy as citizen
registration, and passes it on out of band. A forced change on first login
(`must_change_password`, TRD §8) needs a migration and is not built.

Nothing here logs an email address, a name, a password or a token — only ids.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, func, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.exceptions import BadRequestError, ConflictError, NotFoundError, WeftException
from app.core.permissions import Role
from app.core.security import hash_password
from app.models.authority_user import AuthorityUser
from app.models.authority_zone import AuthorityZone
from app.models.department import Department
from app.models.issue import Issue
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.models.zone import Zone
from app.schemas.admin import (
    MAX_BOUNDARY_POSITIONS,
    CreateAuthorityRequest,
    CreateDepartmentRequest,
    CreateZoneRequest,
    GeoJSONPolygon,
    UpdateDepartmentRequest,
)

logger = logging.getLogger(__name__)

# Arbitrary but fixed key for `pg_advisory_xact_lock`, serialising department
# creation so the case-insensitive name check cannot race (see
# `create_department`). Only this module takes it.
_DEPARTMENT_NAME_LOCK_KEY = 0x5745_4654_4445_5054  # "WEFTDEPT"


@dataclass(frozen=True)
class AuthorityUserFilters:
    """Everything `GET /admin/authority-users` can filter on. All optional."""

    department_id: uuid.UUID | None = None
    is_active: bool | None = None


# ── Authority accounts: reads ───────────────────────────────────────────


def _authority_columns() -> Select:
    """The exact columns an admin may see of an authority account.

    Columns, not ORM entities, on purpose. Selecting `User` would read
    `password_hash` into memory on every list call, and — because the
    relationships are `lazy="selectin"` — cascade into profile, department,
    zone and category loads nobody asked for. Naming the columns makes the
    exposed surface reviewable in one place.
    """
    return (
        select(
            AuthorityUser.id,
            AuthorityUser.user_id,
            User.email,
            User.name,
            User.role,
            User.is_active,
            AuthorityUser.employee_id,
            AuthorityUser.department_id,
            Department.name.label("department_name"),
            AuthorityUser.designation,
            AuthorityUser.is_dept_admin,
            AuthorityUser.created_at,
        )
        .join(User, User.id == AuthorityUser.user_id)
        .join(Department, Department.id == AuthorityUser.department_id)
    )


async def _with_zone_ids(db: AsyncSession, rows: list[Any]) -> list[dict[str, Any]]:
    """Attach each profile's zone ids, in one query for the whole page."""
    profile_ids = [row.id for row in rows]
    zone_map: dict[uuid.UUID, list[uuid.UUID]] = {pid: [] for pid in profile_ids}
    if profile_ids:
        links = await db.execute(
            select(AuthorityZone.authority_user_id, AuthorityZone.zone_id)
            .where(AuthorityZone.authority_user_id.in_(profile_ids))
            .order_by(AuthorityZone.authority_user_id, AuthorityZone.zone_id)
        )
        for profile_id, zone_id in links.all():
            zone_map[profile_id].append(zone_id)
    return [{**row._mapping, "zone_ids": zone_map[row.id]} for row in rows]


async def list_authority_users(
    db: AsyncSession,
    *,
    filters: AuthorityUserFilters,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict[str, Any]], int]:
    """One page of authority accounts plus the total matching count.

    Ordered by name, then id, so a staff picker reads alphabetically and paging
    is stable when two people share a name.
    """
    base = _authority_columns()
    if filters.department_id is not None:
        base = base.where(AuthorityUser.department_id == filters.department_id)
    if filters.is_active is not None:
        base = base.where(User.is_active.is_(filters.is_active))

    total = await db.scalar(select(func.count()).select_from(base.subquery())) or 0
    stmt = base.order_by(User.name, AuthorityUser.id).offset((page - 1) * page_size).limit(page_size)
    rows = list((await db.execute(stmt)).all())
    return await _with_zone_ids(db, rows), total


async def get_authority_user(db: AsyncSession, *, user_id: uuid.UUID) -> dict[str, Any]:
    """One authority account by its *user* id, or raise 404."""
    row = (await db.execute(_authority_columns().where(AuthorityUser.user_id == user_id))).first()
    if row is None:
        raise NotFoundError("Authority user")
    return (await _with_zone_ids(db, [row]))[0]


# ── Authority accounts: writes ──────────────────────────────────────────


def _authority_conflict(exc: IntegrityError) -> ConflictError | None:
    """Map a unique violation to its 409, or None if it is something else."""
    detail = str(exc.orig)
    if "users_email_key" in detail:
        return ConflictError(code="EMAIL_ALREADY_REGISTERED", message="Email already registered")
    if "authority_users_employee_id_key" in detail:
        return ConflictError(code="EMPLOYEE_ID_ALREADY_EXISTS", message="Employee ID already in use")
    return None


async def _require_zones(db: AsyncSession, zone_ids: list[uuid.UUID]) -> None:
    """Raise 404 naming every requested zone that does not exist."""
    if not zone_ids:
        return
    found = set((await db.scalars(select(Zone.id).where(Zone.id.in_(zone_ids)))).all())
    missing = [str(z) for z in zone_ids if z not in found]
    if missing:
        # NotFoundError carries no details; the caller needs to know which ids.
        raise WeftException(404, "NOT_FOUND", "Zone not found", {"missing_zone_ids": missing})


async def create_authority_user(db: AsyncSession, payload: CreateAuthorityRequest, *, actor: User) -> dict[str, Any]:
    """Provision an AUTHORITY account: the `users` row, its profile, its zones.

    All three writes share one SAVEPOINT, so a conflict on any of them leaves no
    half-made account behind and the caller's transaction intact.

    Raises:
        NotFoundError: the department or any zone does not exist.
        BadRequestError: the department is inactive.
        ConflictError: the email or the employee ID is already taken.
    """
    department_active = await db.scalar(select(Department.is_active).where(Department.id == payload.department_id))
    if department_active is None:
        raise NotFoundError("Department")
    if not department_active:
        raise BadRequestError(code="DEPARTMENT_INACTIVE", message="Cannot add staff to an inactive department")

    zone_ids = list(dict.fromkeys(payload.zone_ids))  # de-duplicate, keep order
    await _require_zones(db, zone_ids)

    # The provisioning admin's own profile, if they have one. Read by query, not
    # via `actor.authority_profile`: the actor may be an object this session
    # created without loading that relationship, and touching an unloaded
    # relationship under asyncio raises rather than lazy-loading.
    creator_id = await db.scalar(select(AuthorityUser.id).where(AuthorityUser.user_id == actor.id))

    # Hashed before the SAVEPOINT opens: bcrypt is ~180ms of CPU, and there is
    # no reason to hold a savepoint (and its row locks) open across it.
    password_hash = hash_password(payload.password)

    user = User(
        id=uuid.uuid4(),
        email=payload.email,
        name=payload.name,
        password_hash=password_hash,
        role=Role.AUTHORITY.value,
        is_anonymous=False,
        is_active=True,
    )
    profile = AuthorityUser(
        id=uuid.uuid4(),
        user_id=user.id,
        department_id=payload.department_id,
        employee_id=payload.employee_id,
        designation=payload.designation,
        is_dept_admin=False,
        created_by=creator_id,
    )

    # Two flushes: `users` must exist before `authority_users` can reference it.
    savepoint = await db.begin_nested()
    try:
        db.add(user)
        await db.flush()
        db.add(profile)
        db.add_all([AuthorityZone(authority_user_id=profile.id, zone_id=z) for z in zone_ids])
        await db.flush()
    except IntegrityError as exc:
        await savepoint.rollback()
        conflict = _authority_conflict(exc)
        if conflict is None:
            raise
        raise conflict from exc
    await savepoint.commit()

    logger.info(
        "Authority account created user_id=%s profile_id=%s department=%s zones=%d by=%s",
        user.id,
        profile.id,
        payload.department_id,
        len(zone_ids),
        actor.id,
    )
    return await get_authority_user(db, user_id=user.id)


async def deactivate_authority_user(db: AsyncSession, *, user_id: uuid.UUID, actor: User) -> dict[str, Any]:
    """Deactivate an authority account and revoke every one of its sessions.

    Idempotent: deactivating an already-deactivated account succeeds and
    changes nothing visible. Its refresh tokens are revoked again regardless —
    a no-op unless one slipped in, which is exactly when it matters.

    The account is never deleted (PRD US-011): its issue assignments and audit
    history keep pointing at a real row.

    Raises:
        BadRequestError: the admin is trying to deactivate their own account.
        NotFoundError: no authority account has this user id.
    """
    if user_id == actor.id:
        # Checked first, before any lookup: whatever the target is, refusing
        # this one is never wrong, and an admin locked out of their own
        # account cannot undo it.
        raise BadRequestError(code="CANNOT_DEACTIVATE_SELF", message="You cannot deactivate your own account")

    if await db.scalar(select(AuthorityUser.id).where(AuthorityUser.user_id == user_id)) is None:
        # Citizens included: this endpoint manages authority accounts only.
        raise NotFoundError("Authority user")

    # Guarded on is_active so a repeat call does not bump `updated_at` on an
    # account that was already off.
    await db.execute(update(User).where(User.id == user_id, User.is_active.is_(True)).values(is_active=False))

    # Same transaction as the flag. Access tokens cannot be revoked (they are
    # stateless) but `get_current_user` re-checks `is_active` on every request,
    # so they die with the flag. Refresh tokens are revoked outright, so
    # reactivating the account later does not resurrect its old sessions.
    revoked = await db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.is_revoked.is_(False))
        .values(is_revoked=True)
    )
    await db.flush()

    logger.info(
        "Authority account deactivated user_id=%s refresh_tokens_revoked=%d by=%s",
        user_id,
        revoked.rowcount,
        actor.id,
    )
    return await get_authority_user(db, user_id=user_id)


# ── Departments ─────────────────────────────────────────────────────────


def _department_query() -> Select:
    # populate_existing: a department already in the session's identity map
    # (e.g. loaded earlier in the request) would otherwise be returned with
    # whatever `categories` it had then, not what the database holds now.
    return (
        select(Department)
        .options(selectinload(Department.categories))
        .execution_options(populate_existing=True)
        .order_by(Department.code)
    )


def department_categories(department: Department) -> list[str]:
    """The categories routed to a department, sorted for a stable response."""
    return sorted(c.category for c in department.categories)


async def list_departments(db: AsyncSession) -> list[Department]:
    """Every department, active or not, ordered by code."""
    return list((await db.scalars(_department_query())).all())


async def get_department(db: AsyncSession, department_id: uuid.UUID) -> Department:
    """One department with its categories loaded, or raise 404."""
    department = await db.scalar(_department_query().where(Department.id == department_id))
    if department is None:
        raise NotFoundError("Department")
    return department


async def create_department(db: AsyncSession, payload: CreateDepartmentRequest) -> Department:
    """Create a department with no category routing.

    `code` is unique in the schema, so its collision is caught from the
    constraint inside a SAVEPOINT. `name` is not (migration 003), so a check
    alone would race: two admins creating "Roads" at once would both pass it.
    A transaction-scoped advisory lock serialises department creation instead.
    It is released at commit, costs nothing on a table that changes a few times
    a year, and needs no migration. A `UNIQUE (lower(name))` index would make
    the lock unnecessary.

    Raises:
        ConflictError: the name (case-insensitively) or the code is taken.
    """
    await db.execute(select(func.pg_advisory_xact_lock(_DEPARTMENT_NAME_LOCK_KEY)))

    taken = await db.scalar(select(Department.id).where(func.lower(Department.name) == payload.name.lower()))
    if taken is not None:
        raise ConflictError(code="DEPARTMENT_NAME_EXISTS", message="A department with this name already exists")

    department = Department(
        name=payload.name,
        code=payload.code,
        description=payload.description,
        contact_email=payload.contact_email,
        sla_hours=payload.sla_hours,
        upvote_alert_threshold=payload.upvote_alert_threshold,
        is_active=True,
        # Initialised so the response can read it without a lazy load, which
        # asyncio forbids. A new department routes nothing until mapped.
        categories=[],
    )
    savepoint = await db.begin_nested()
    db.add(department)
    try:
        await db.flush()
    except IntegrityError as exc:
        await savepoint.rollback()
        if "departments_code_key" not in str(exc.orig):
            raise
        raise ConflictError(
            code="DEPARTMENT_CODE_EXISTS", message="A department with this code already exists"
        ) from exc
    await savepoint.commit()

    logger.info("Department created department_id=%s code=%s", department.id, department.code)
    return department


async def update_department(
    db: AsyncSession, *, department_id: uuid.UUID, payload: UpdateDepartmentRequest
) -> Department:
    """Change a department's SLA and/or upvote alert threshold (task 4.15a).

    Only the fields the client sent are touched; `UpdateDepartmentRequest`
    has already refused nulls and values below 1. The threshold is read fresh
    from the row on every check (task 1.27), so a change takes effect on the
    next upvote with no restart.
    """
    department = await get_department(db, department_id)
    for field in sorted(payload.model_fields_set):
        setattr(department, field, getattr(payload, field))
    await db.flush()

    logger.info(
        "Department updated department_id=%s fields=%s",
        department.id,
        ",".join(sorted(payload.model_fields_set)),
    )
    return department


# ── Zones ───────────────────────────────────────────────────────────────


def _invalid_geometry(reason: str) -> BadRequestError:
    return BadRequestError(
        code="INVALID_GEOMETRY", message=f"Invalid zone boundary: {reason}", details={"reason": reason}
    )


def _check_polygon_shape(boundary: GeoJSONPolygon) -> None:
    """Everything about a polygon that can be checked without PostGIS.

    Done in Python first so the common mistakes get a specific message, and so
    nothing malformed reaches `ST_GeomFromGeoJSON`, whose failures are opaque.
    """
    rings = boundary.coordinates
    if not rings:
        raise _invalid_geometry("a polygon needs an exterior ring")

    total = sum(len(ring) for ring in rings)
    if total > MAX_BOUNDARY_POSITIONS:
        raise _invalid_geometry(f"at most {MAX_BOUNDARY_POSITIONS} positions are accepted, got {total}")

    for index, ring in enumerate(rings):
        label = "exterior ring" if index == 0 else f"interior ring {index}"
        if len(ring) < 4:
            raise _invalid_geometry(f"{label} has {len(ring)} positions; a closed ring needs at least 4")
        for position in ring:
            if len(position) != 2:
                # The column is 2D. A third (altitude) value would make a
                # POLYGON Z that the column refuses with an opaque error.
                raise _invalid_geometry(f"{label}: positions must be [longitude, latitude], got {position}")
            longitude, latitude = position
            # Written as a positive range test so NaN, which compares false to
            # everything, fails it too.
            if not (-180.0 <= longitude <= 180.0 and -90.0 <= latitude <= 90.0):
                raise _invalid_geometry(
                    f"{label}: position {position} is out of range "
                    "(longitude must be within ±180, latitude within ±90; GeoJSON order is [lng, lat])"
                )
        if ring[0] != ring[-1]:
            raise _invalid_geometry(f"{label} is not closed; its first and last positions must be equal")


async def _validated_ewkt(db: AsyncSession, boundary: GeoJSONPolygon) -> str:
    """Ask PostGIS whether the polygon is valid; return it as EWKT if so.

    Run in a SAVEPOINT: if PostGIS raises on input the Python checks did not
    anticipate, the error aborts only the savepoint, not the caller's
    transaction, and becomes a 400 rather than a 500.
    """
    geojson = json.dumps({"type": "Polygon", "coordinates": boundary.coordinates})
    savepoint = await db.begin_nested()
    try:
        row = (
            await db.execute(
                text(
                    """
                    SELECT ST_IsValid(g) AS valid, ST_IsValidReason(g) AS reason, ST_AsEWKT(g) AS ewkt
                    FROM (SELECT ST_SetSRID(ST_GeomFromGeoJSON(CAST(:geojson AS text)), 4326) AS g) AS candidate
                    """
                ),
                {"geojson": geojson},
            )
        ).one()
    except DBAPIError as exc:
        await savepoint.rollback()
        raise _invalid_geometry("PostGIS could not parse the polygon") from exc
    await savepoint.commit()

    if not row.valid:
        # e.g. "Self-intersection[77.59 12.97]" — PostGIS's own words, which
        # point at the offending vertex.
        raise _invalid_geometry(row.reason)
    return row.ewkt


def _zone_columns() -> Select:
    return select(
        Zone.id,
        Zone.name,
        Zone.department_id,
        Zone.is_active,
        Zone.created_at,
        func.ST_AsGeoJSON(Zone.boundary).label("boundary_geojson"),
    )


def _zone_dict(row: Any) -> dict[str, Any]:
    data = dict(row._mapping)
    data["boundary"] = json.loads(data.pop("boundary_geojson"))
    return data


async def list_zones(db: AsyncSession) -> list[dict[str, Any]]:
    """Every zone, active or not, with its boundary as GeoJSON, ordered by name."""
    rows = (await db.execute(_zone_columns().order_by(Zone.name, Zone.id))).all()
    return [_zone_dict(row) for row in rows]


async def create_zone(db: AsyncSession, payload: CreateZoneRequest) -> dict[str, Any]:
    """Create a zone from a validated GeoJSON polygon.

    Overlapping an existing zone is permitted: `issue_service._resolve_zone`
    already resolves a point in two zones deterministically, by name.

    Raises:
        BadRequestError: `INVALID_GEOMETRY`, with the reason.
        NotFoundError: `department_id` was given and does not exist.
    """
    if (
        payload.department_id is not None
        and await db.scalar(select(Department.id).where(Department.id == payload.department_id)) is None
    ):
        raise NotFoundError("Department")

    _check_polygon_shape(payload.boundary)
    ewkt = await _validated_ewkt(db, payload.boundary)

    zone = Zone(name=payload.name, department_id=payload.department_id, boundary=ewkt, is_active=True)
    db.add(zone)
    await db.flush()

    logger.info("Zone created zone_id=%s department=%s", zone.id, payload.department_id)
    row = (await db.execute(_zone_columns().where(Zone.id == zone.id))).one()
    return _zone_dict(row)


# ── System stats ────────────────────────────────────────────────────────


async def system_stats(db: AsyncSession) -> dict[str, int]:
    """Six row counts in one round trip.

    Cheap at municipal scale: each is a plain COUNT over a small table or an
    indexed column, folded into a single SELECT of scalar subqueries.
    """
    active_authority = (
        select(func.count())
        .select_from(AuthorityUser)
        .join(User, User.id == AuthorityUser.user_id)
        .where(User.is_active.is_(True))
    )
    counts = {
        "total_users": select(func.count()).select_from(User),
        "total_issues": select(func.count()).select_from(Issue),
        "total_resolved": select(func.count()).select_from(Issue).where(Issue.status == "RESOLVED"),
        "active_authority_users": active_authority,
        "departments": select(func.count()).select_from(Department).where(Department.is_active.is_(True)),
        "zones": select(func.count()).select_from(Zone).where(Zone.is_active.is_(True)),
    }
    stmt = select(*(query.scalar_subquery().label(name) for name, query in counts.items()))
    row = (await db.execute(stmt)).one()
    return {name: int(value) for name, value in row._mapping.items()}
