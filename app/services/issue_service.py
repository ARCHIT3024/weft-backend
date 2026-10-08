"""Issue lifecycle business logic — submission, retrieval, triage and upvotes.

The routers stay thin: they translate HTTP into these calls and back. Every
query that touches PostGIS or the status machine lives here.

Four properties this module exists to guarantee:

* **Submission never silently loses routing.** Every issue gets a zone
  (point-in-polygon) and a department (category lookup) resolved at write time,
  and both lookups are deterministic — see `_resolve_department`.
* **The status machine is explicit.** Transitions are checked against
  `LEGAL_TRANSITIONS`, not against ad-hoc `if` statements scattered across
  routers, and every accepted move writes an audit row in the same transaction.
* **The upvote counter is never written from Python.** `issues.upvote_count` is
  owned by the `trg_upvote_count` trigger (migration 012). This module inserts
  and deletes `upvotes` rows and re-reads the counter.
* **`issues.location` is never written.** It is a STORED GENERATED column
  derived from `longitude`/`latitude`, so writing it is not merely redundant,
  it is rejected by PostgreSQL.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from geoalchemy2 import Geography
from sqlalchemy import Select, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.exceptions import BadRequestError, ConflictError, NotFoundError
from app.models.authority_user import AuthorityUser
from app.models.department import Department
from app.models.department_category import DepartmentCategory
from app.models.issue import Issue
from app.models.issue_image import IssueImage
from app.models.issue_status_history import IssueStatusHistory
from app.models.upvote import Upvote
from app.models.user import User
from app.models.zone import Zone

logger = logging.getLogger(__name__)

# ── Status machine ──────────────────────────────────────────────────────
#
# Declared as data, not as branching, so the legal moves can be read in one
# place and asserted directly by tests.
#
# REPORTED   → triage can start it, finish it outright, or reject it as spam.
# IN_PROGRESS→ finish or reject.
# RESOLVED   → **reopening is allowed**, back to IN_PROGRESS only. A municipal
#              fix that did not hold is a real and common case, and forcing a
#              citizen to file a duplicate report would corrupt both the
#              duplicate metrics and the resolution-time statistics. It cannot
#              go straight back to REPORTED: the issue has been triaged, and
#              pretending otherwise would lose that history.
# REJECTED   → terminal. Reversing a rejection is an admin data-correction
#              concern, not a triage transition, and is deliberately not
#              reachable through this endpoint.
LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    "REPORTED": frozenset({"IN_PROGRESS", "RESOLVED", "REJECTED"}),
    "IN_PROGRESS": frozenset({"RESOLVED", "REJECTED"}),
    "RESOLVED": frozenset({"IN_PROGRESS"}),
    "REJECTED": frozenset(),
}

# `issue_number` is String(20). "ISS-" + 4-digit year + "-" + 10 chars = 19.
_ISSUE_NUMBER_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no I/O/0/1
_ISSUE_NUMBER_RANDOM_LEN = 10
_ISSUE_NUMBER_MAX_ATTEMPTS = 5

# Guard rails for the spatial query. A radius large enough to match the whole
# table turns a GIST index scan into a sequential scan plus a sort.
MAX_NEARBY_LIMIT = 200


@dataclass(frozen=True)
class IssueFilters:
    """Everything `GET /issues` can filter on. All optional."""

    categories: list[str] | None = None
    statuses: list[str] | None = None
    zone_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    reporter_id: uuid.UUID | None = None
    min_upvotes: int | None = None
    from_date: datetime | None = None
    to_date: datetime | None = None
    latitude: float | None = None
    longitude: float | None = None
    radius_m: float | None = None


def generate_issue_number(now: datetime | None = None) -> str:
    """Build a human-quotable reference like `ISS-2026-K7QD3M8XPZ`.

    Random rather than sequential, deliberately. A sequential counter needs
    either a serialised allocation (a lock on every submission) or a separate
    sequence, and it leaks the total submission volume to anyone who files two
    reports — a number that a municipality may not want public.

    32^10 is ~10^15 values per year, so a collision is vanishingly unlikely; the
    unique constraint plus the retry loop in `create_issue` is what makes it
    *correct* rather than merely improbable.
    """
    moment = now or datetime.now(UTC)
    suffix = "".join(secrets.choice(_ISSUE_NUMBER_ALPHABET) for _ in range(_ISSUE_NUMBER_RANDOM_LEN))
    return f"ISS-{moment.year}-{suffix}"


async def _resolve_zone(db: AsyncSession, latitude: float, longitude: float) -> uuid.UUID | None:
    """Return the zone whose boundary contains the point, or None.

    Runs before the insert because `issues.location` does not exist until the
    row does — the generated column is computed by the database on write.

    `ST_Contains` (not `ST_Intersects`) so a point exactly on a shared edge
    between two adjacent zones does not match both. Ordered by name so that if
    overlapping zones are ever configured, the same point always resolves to the
    same zone instead of varying by physical row order.
    """
    stmt = (
        select(Zone.id)
        .where(
            Zone.is_active.is_(True),
            func.ST_Contains(
                Zone.boundary,
                func.ST_SetSRID(func.ST_MakePoint(longitude, latitude), 4326),
            ),
        )
        .order_by(Zone.name)
        .limit(1)
    )
    return await db.scalar(stmt)


async def _resolve_department(db: AsyncSession, category: str) -> uuid.UUID | None:
    """Return the department this category routes to, or None.

    **The ORDER BY is load-bearing, not cosmetic.** `department_categories` has
    a composite primary key `(department_id, category)`, so the schema permits
    one category mapped to several departments. Migration 014 seeds each
    category exactly once, but nothing in the database enforces that. Without a
    deterministic order, two identical submissions could route to different
    departments the moment an operator adds a second mapping — a bug that would
    surface as unreproducible misrouting long after the mapping was added.
    """
    stmt = (
        select(Department.id)
        .join(DepartmentCategory, DepartmentCategory.department_id == Department.id)
        .where(DepartmentCategory.category == category, Department.is_active.is_(True))
        .order_by(Department.code)
        .limit(1)
    )
    return await db.scalar(stmt)


# ── Submission ──────────────────────────────────────────────────────────


async def create_issue(
    db: AsyncSession,
    *,
    category: str,
    latitude: float,
    longitude: float,
    description: str | None = None,
    address_text: str | None = None,
    reporter: User | None = None,
) -> Issue:
    """Create an issue, routing it to a zone and a department.

    `reporter` is None for anonymous submissions — a product requirement, not an
    oversight: the lowest-friction path to reporting a hazard matters more than
    attribution, so `reporter_id` stays NULL.

    Writes the opening `REPORTED` row of the audit trail in the same
    transaction, so an issue can never exist without its history.

    Images are attached separately by the caller via `attach_image`, because
    storing them can fail independently and must not roll back a valid report.
    """
    zone_id = await _resolve_zone(db, latitude, longitude)
    department_id = await _resolve_department(db, category)

    if department_id is None:
        # Routing is seeded for all seven categories, so this means the seed
        # migration has not run. Loud, because the alternative is a silently
        # unrouted issue that no department will ever see.
        logger.warning("No department routing for category=%s — issue will be unrouted", category)

    issue = Issue(
        issue_number=generate_issue_number(),
        reporter_id=reporter.id if reporter is not None else None,
        category=category,
        description=description,
        address_text=address_text,
        # Numeric(10, 7) columns: hand Decimal to the driver rather than float,
        # so the stored value is the one that was submitted.
        latitude=Decimal(str(latitude)),
        longitude=Decimal(str(longitude)),
        zone_id=zone_id,
        department_id=department_id,
        status="REPORTED",
    )

    # Each attempt runs inside a SAVEPOINT. A bare `db.rollback()` here would
    # discard the caller's entire transaction — everything written earlier in
    # the request, not just this failed INSERT — and then retry inside a
    # transaction that no longer exists. The savepoint undoes exactly the
    # statement that failed.
    for attempt in range(_ISSUE_NUMBER_MAX_ATTEMPTS):
        savepoint = await db.begin_nested()
        db.add(issue)
        try:
            await db.flush()
        except IntegrityError as exc:
            await savepoint.rollback()
            # Only an issue_number collision is retryable. Anything else (a bad
            # FK, a violated check) is a real error and must not be swallowed.
            if "issue_number" not in str(exc.orig):
                raise
            if attempt == _ISSUE_NUMBER_MAX_ATTEMPTS - 1:
                raise ConflictError(
                    code="ISSUE_NUMBER_COLLISION",
                    message="Could not allocate a unique issue number. Please retry.",
                ) from exc
            issue.issue_number = generate_issue_number()
            logger.warning("issue_number collision, retrying (attempt %d)", attempt + 1)
        else:
            await savepoint.commit()
            break

    db.add(
        IssueStatusHistory(
            issue_id=issue.id,
            previous_status=None,  # NULL exactly once per issue: the opening row
            new_status="REPORTED",
            changed_by_id=reporter.id if reporter is not None else None,
            note="Issue submitted",
        )
    )
    await db.flush()

    logger.info(
        "Issue created issue_id=%s number=%s zone=%s department=%s anonymous=%s",
        issue.id,
        issue.issue_number,
        zone_id,
        department_id,
        reporter is None,
    )
    return issue


async def attach_image(
    db: AsyncSession,
    *,
    issue_id: uuid.UUID,
    file_path: str,
    image_type: str = "REPORT",
) -> IssueImage:
    """Record a stored image against an issue."""
    image = IssueImage(issue_id=issue_id, file_path=file_path, image_type=image_type)
    db.add(image)
    await db.flush()
    return image


async def count_images(db: AsyncSession, issue_id: uuid.UUID) -> int:
    """How many images an issue already carries — enforces the per-issue cap."""
    return await db.scalar(select(func.count()).select_from(IssueImage).where(IssueImage.issue_id == issue_id)) or 0


# ── Retrieval ───────────────────────────────────────────────────────────


def _apply_filters(stmt: Select, filters: IssueFilters) -> Select:
    """Narrow a SELECT by every filter that was actually supplied."""
    if filters.categories:
        stmt = stmt.where(Issue.category.in_(filters.categories))
    if filters.statuses:
        stmt = stmt.where(Issue.status.in_(filters.statuses))
    if filters.zone_id is not None:
        stmt = stmt.where(Issue.zone_id == filters.zone_id)
    if filters.department_id is not None:
        stmt = stmt.where(Issue.department_id == filters.department_id)
    if filters.reporter_id is not None:
        stmt = stmt.where(Issue.reporter_id == filters.reporter_id)
    if filters.min_upvotes is not None:
        stmt = stmt.where(Issue.upvote_count >= filters.min_upvotes)
    if filters.from_date is not None:
        stmt = stmt.where(Issue.created_at >= filters.from_date)
    if filters.to_date is not None:
        stmt = stmt.where(Issue.created_at <= filters.to_date)

    # Spatial narrowing only when the caller gave a complete point + radius.
    if filters.latitude is not None and filters.longitude is not None and filters.radius_m:
        stmt = stmt.where(_within(filters.latitude, filters.longitude, filters.radius_m))
    return stmt


def _within(latitude: float, longitude: float, radius_m: float):  # noqa: ANN202 — SQLAlchemy clause
    """`ST_DWithin` predicate in metres against the GIST-indexed point column.

    `issues.location` is GEOMETRY/4326, whose units are degrees, so a plain
    `ST_DWithin` would take a radius in degrees — which is not a distance, since
    a degree of longitude shrinks from ~111 km at the equator to nothing at the
    poles. Casting both sides to `geography` gives real metres. The cast is
    index-usable: PostGIS rewrites it to a bounding-box search that the GIST
    index on `location` still serves.
    """
    point = func.ST_SetSRID(func.ST_MakePoint(longitude, latitude), 4326)
    return func.ST_DWithin(
        func.cast(Issue.location, Geography),
        func.cast(point, Geography),
        radius_m,
    )


async def list_issues(
    db: AsyncSession,
    *,
    filters: IssueFilters,
    page: int = 1,
    page_size: int = 20,
    sort_field: str = "created_at",
    descending: bool = True,
) -> tuple[list[Issue], int]:
    """Return one page of issues plus the total matching count."""
    base = _apply_filters(select(Issue), filters)

    total = await db.scalar(select(func.count()).select_from(base.subquery())) or 0

    column = {
        "created_at": Issue.created_at,
        "updated_at": Issue.updated_at,
        "upvote_count": Issue.upvote_count,
        "status": Issue.status,
    }.get(sort_field, Issue.created_at)

    stmt = (
        base.order_by(column.desc() if descending else column.asc(), Issue.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = (await db.scalars(stmt)).unique().all()
    return list(rows), total


async def get_issue(db: AsyncSession, issue_id: uuid.UUID) -> Issue:
    """Return one issue with its images eagerly loaded, or raise 404."""
    stmt = select(Issue).where(Issue.id == issue_id).options(selectinload(Issue.images))
    issue = await db.scalar(stmt)
    if issue is None:
        raise NotFoundError("Issue")
    return issue


async def get_status_history(db: AsyncSession, issue_id: uuid.UUID) -> list[IssueStatusHistory]:
    """Full audit trail for an issue, newest first."""
    stmt = (
        select(IssueStatusHistory)
        .where(IssueStatusHistory.issue_id == issue_id)
        .order_by(IssueStatusHistory.created_at.desc())
    )
    return list((await db.scalars(stmt)).all())


async def find_nearby(
    db: AsyncSession,
    *,
    latitude: float,
    longitude: float,
    radius_m: float,
    limit: int = 50,
    statuses: list[str] | None = None,
) -> list[tuple[Issue, float]]:
    """Issues within `radius_m` of the point, nearest first, with distances.

    Distance is computed in the same `geography` space as the filter, so the
    number returned is the one the predicate used — metres on the spheroid, not
    a degree measurement that happens to look like a distance.
    """
    point = func.ST_SetSRID(func.ST_MakePoint(longitude, latitude), 4326)
    distance = func.ST_Distance(func.cast(Issue.location, Geography), func.cast(point, Geography))

    stmt = select(Issue, distance.label("distance_m")).where(_within(latitude, longitude, radius_m))
    if statuses:
        stmt = stmt.where(Issue.status.in_(statuses))
    stmt = stmt.order_by(distance).limit(min(limit, MAX_NEARBY_LIMIT))

    result = await db.execute(stmt)
    return [(row[0], float(row[1])) for row in result.all()]


# ── Triage ──────────────────────────────────────────────────────────────


async def update_status(
    db: AsyncSession,
    *,
    issue_id: uuid.UUID,
    new_status: str,
    actor: User,
    note: str | None = None,
) -> Issue:
    """Move an issue to `new_status`, recording the transition.

    Raises:
        NotFoundError: no such issue.
        BadRequestError: the move is not in `LEGAL_TRANSITIONS`, including the
            no-op case of transitioning to the status the issue already has —
            which is rejected rather than ignored so the audit trail does not
            accumulate rows that record nothing happening.
    """
    issue = await get_issue(db, issue_id)
    previous = issue.status

    if new_status == previous:
        raise BadRequestError(
            code="INVALID_STATUS_TRANSITION",
            message=f"Issue is already {previous}.",
            details={"current_status": previous, "requested_status": new_status},
        )
    if new_status not in LEGAL_TRANSITIONS.get(previous, frozenset()):
        raise BadRequestError(
            code="INVALID_STATUS_TRANSITION",
            message=f"Cannot move an issue from {previous} to {new_status}.",
            details={
                "current_status": previous,
                "requested_status": new_status,
                "allowed": sorted(LEGAL_TRANSITIONS.get(previous, frozenset())),
            },
        )

    issue.status = new_status
    if new_status == "RESOLVED":
        issue.resolved_at = datetime.now(UTC)
        issue.resolution_note = note
    elif previous == "RESOLVED":
        # Reopened: the previous resolution no longer holds, and leaving
        # `resolved_at` set would silently corrupt resolution-time analytics.
        issue.resolved_at = None
        issue.resolution_note = None

    db.add(
        IssueStatusHistory(
            issue_id=issue.id,
            previous_status=previous,
            new_status=new_status,
            changed_by_id=actor.id,
            note=note,
        )
    )
    await db.flush()

    logger.info("Issue status changed issue_id=%s %s -> %s by=%s", issue.id, previous, new_status, actor.id)
    return issue


async def assign_issue(
    db: AsyncSession,
    *,
    issue_id: uuid.UUID,
    assigned_to_id: uuid.UUID,
    actor: User,
) -> Issue:
    """Assign an issue to an authority staff member."""
    issue = await get_issue(db, issue_id)

    assignee = await db.get(AuthorityUser, assigned_to_id)
    if assignee is None:
        raise NotFoundError("Authority user")

    issue.assigned_to_id = assigned_to_id
    issue.assigned_at = datetime.now(UTC)
    await db.flush()

    logger.info("Issue assigned issue_id=%s to=%s by=%s", issue.id, assigned_to_id, actor.id)
    return issue


# ── Upvotes ─────────────────────────────────────────────────────────────


async def add_upvote(db: AsyncSession, *, issue_id: uuid.UUID, user: User) -> Issue:
    """Record one upvote, or raise 409 if this user already upvoted.

    Duplicate defence is the composite primary key, not a prior SELECT: two
    concurrent requests both passing a "have they voted?" check is exactly the
    race that produces a double count, and the database is the only place that
    can settle it.

    `issues.upvote_count` is deliberately not touched here — the
    `trg_upvote_count` trigger owns it. The issue is re-read afterwards so the
    caller sees the trigger's value rather than a stale one.
    """
    issue = await get_issue(db, issue_id)

    # SAVEPOINT, not a full rollback: a duplicate vote is an expected outcome,
    # not a transaction-ending error, and discarding the caller's whole
    # transaction to report it would be collateral damage.
    savepoint = await db.begin_nested()
    db.add(Upvote(user_id=user.id, issue_id=issue_id))
    try:
        await db.flush()
    except IntegrityError as exc:
        await savepoint.rollback()
        raise ConflictError(
            code="ALREADY_UPVOTED",
            message="You have already upvoted this issue.",
        ) from exc
    await savepoint.commit()

    await db.refresh(issue)
    return issue


async def remove_upvote(db: AsyncSession, *, issue_id: uuid.UUID, user: User) -> Issue:
    """Withdraw this user's upvote. Absent upvote is not an error.

    Idempotent for the same reason logout is (D-3): the caller's intent is "I
    should not be counted", and that is satisfied either way. A 404 here would
    also leak whether a given user had upvoted a given issue.
    """
    issue = await get_issue(db, issue_id)
    await db.execute(delete(Upvote).where(Upvote.user_id == user.id, Upvote.issue_id == issue_id))
    await db.flush()
    await db.refresh(issue)
    return issue


async def has_upvoted(db: AsyncSession, *, issue_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    """Whether this user has already upvoted this issue."""
    stmt = select(Upvote.user_id).where(Upvote.user_id == user_id, Upvote.issue_id == issue_id)
    return await db.scalar(stmt) is not None


async def upvote_timestamp(db: AsyncSession, *, issue_id: uuid.UUID, user_id: uuid.UUID) -> datetime:
    """When this user upvoted this issue.

    Read back from the row rather than generated in Python: `created_at` carries
    a database `NOW()` default, so a value made up here could disagree with what
    was actually stored. Falls back to now only if the row has vanished between
    the insert and this read, which a concurrent withdrawal can cause.
    """
    stmt = select(Upvote.created_at).where(Upvote.user_id == user_id, Upvote.issue_id == issue_id)
    return await db.scalar(stmt) or datetime.now(UTC)
