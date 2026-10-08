"""Analytics — live KPI aggregation over `issues` and `issue_status_history`.

Every figure is computed **live**, by SQL aggregation in PostgreSQL, at request
time. The `analytics_snapshots` table and its nightly cron are task 4.12 and do
not exist yet; when they land they should be a cache in front of these queries,
not a second definition of the same numbers.

Definitions — this docstring is the single source of truth for each figure:

* **Filters.** The date range selects issues by `issues.created_at` (when the
  report was filed), inclusive at both ends, with the same semantics as
  `GET /issues` — `issue_service._apply_filters` is reused so the two cannot
  drift. Department, zone and category filter the same columns that endpoint
  does. A timezone-less date-time is read as UTC.
* **Scope.** An ADMIN sees every issue. An AUTHORITY sees only issues whose
  `zone_id` is one of their `authority_zones` (TRD RBAC "View all issues in
  zone"; schema doc §6's dashboard query `i.zone_id = ANY(:zone_ids)`). Their
  department is *not* a scope boundary — a supervisor's jurisdiction is
  geographic, and the department is available as an ordinary filter. Issues
  outside every zone are therefore visible to admins only.
* **Resolution time** is wall-clock hours from the report (`issues.created_at`)
  to the resolution that **currently stands**: the latest `RESOLVED` row in
  `issue_status_history`. Only issues whose status is RESOLVED right now count.
  So an issue resolved, reopened (D-12) and resolved again is measured to its
  *final* resolution — the time the citizen actually waited for a fix that
  held, with the failed fix counted against the department rather than
  flattering it. A reopened issue that is not yet re-resolved has no
  resolution time at all. The audit log is used rather than `issues.resolved_at`
  because it is the immutable record, stamped with `clock_timestamp()` (D-13);
  `resolved_at` is only the fallback for a RESOLVED issue with no RESOLVED
  history row, which the service layer never produces.
* **SLA breach** — an issue that is not RESOLVED or REJECTED and whose age
  (`now() - created_at`) exceeds its department's `sla_hours`. An issue with no
  department has no SLA and can never be in breach. A reopened issue's clock
  runs from the original report. An issue resolved late is *not* a breach here:
  the figure answers "what is overdue right now".
* **Heatmap** — see `heatmap`.
"""

from __future__ import annotations

import csv
import io
import logging
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Date, Float, Numeric, Select, and_, case, cast, func, literal, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BadRequestError, WeftException
from app.models.authority_user import AuthorityUser
from app.models.authority_zone import AuthorityZone
from app.models.department import Department
from app.models.issue import Issue
from app.models.issue_status_history import IssueStatusHistory
from app.models.user import User
from app.models.zone import Zone
from app.schemas.analytics import (
    AnalyticsSummary,
    CategoryCount,
    HeatmapPoint,
    HeatmapResponse,
    ResolutionGroupBy,
    ResolutionTimeGroup,
    ResolutionTimesResponse,
    ResolutionTimeStats,
    ResolutionTrendPoint,
    SlaBreach,
    TrendInterval,
)
from app.schemas.issue import IssueCategory
from app.services.issue_service import IssueFilters, _apply_filters

logger = logging.getLogger(__name__)

OPEN_STATUSES = ("REPORTED", "IN_PROGRESS")

# Label for a group whose department or zone is NULL — an unrouted issue, or one
# reported outside every zone.
UNASSIGNED_LABEL = "Unassigned"

# ── Response bounds ─────────────────────────────────────────────────────
#
# Every endpoint aggregates in the database and returns a bounded payload,
# whatever the size of `issues`.

# ~1.1 km at the equator — the TRD §9 heatmap grid.
DEFAULT_HEATMAP_GRID_DEG = 0.01
MIN_HEATMAP_GRID_DEG = 0.001
MAX_HEATMAP_GRID_DEG = 1.0
# At the default grid a whole city occupies a few hundred cells, so this cap is
# not reached in normal use; it exists so that a very fine grid over a large
# scope cannot return an unbounded payload. Leaflet.heat renders this many
# points without effort.
MAX_HEATMAP_POINTS = 2_000

# Thirteen months of daily buckets, or every week/month of a long history.
MAX_TREND_POINTS = 400

# The export is materialised before it is streamed: FastAPI 0.111 tears down
# `yield` dependencies — closing the request's session — before the response
# body is sent, so rows cannot be read lazily while the CSV streams. The cap
# keeps that bounded; beyond it the caller is asked to narrow the filters rather
# than handed a silently truncated file.
EXPORT_MAX_ROWS = 50_000


# ── Scope and filters ───────────────────────────────────────────────────


@dataclass(frozen=True)
class AnalyticsQuery:
    """The filters a caller asked for, plus the jurisdiction they are allowed.

    `scope_zone_ids` is None for an unrestricted (admin) caller, and otherwise
    the authority's zones — possibly empty, in which case every figure is zero.
    """

    scope_zone_ids: frozenset[uuid.UUID] | None
    from_date: datetime | None = None
    to_date: datetime | None = None
    department_id: uuid.UUID | None = None
    zone_id: uuid.UUID | None = None
    categories: tuple[str, ...] | None = None


async def build_query(
    db: AsyncSession,
    *,
    user: User,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    department_id: uuid.UUID | None = None,
    zone_id: uuid.UUID | None = None,
    categories: Sequence[str] | None = None,
) -> AnalyticsQuery:
    """Resolve the caller's scope and validate the requested filters against it.

    `user` must already have passed the staff guard (AUTHORITY or ADMIN).

    Raises:
        BadRequestError: `from_date` is after `to_date` (`INVALID_DATE_RANGE`).
        WeftException: 403 `AUTHORITY_PROFILE_MISSING` for an AUTHORITY account
            with no `authority_users` row — its jurisdiction is undefined, and
            answering with zeros would present a provisioning fault as a quiet
            municipality. 403 `ZONE_OUT_OF_SCOPE` when an authority filters by a
            zone they are not assigned — refused rather than answered with zeros,
            so a mistyped zone id is never mistaken for an empty one.
    """
    from_date, to_date = _as_utc(from_date), _as_utc(to_date)
    if from_date is not None and to_date is not None and from_date > to_date:
        raise BadRequestError(
            code="INVALID_DATE_RANGE",
            message="from_date must not be after to_date.",
            details={"from_date": from_date.isoformat(), "to_date": to_date.isoformat()},
        )

    scope = await _scope_for(db, user)
    if scope is not None and zone_id is not None and zone_id not in scope:
        raise WeftException(403, "ZONE_OUT_OF_SCOPE", "You are not assigned to that zone.", {"zone_id": str(zone_id)})

    return AnalyticsQuery(
        scope_zone_ids=scope,
        from_date=from_date,
        to_date=to_date,
        department_id=department_id,
        zone_id=zone_id,
        categories=tuple(categories) if categories else None,
    )


async def _scope_for(db: AsyncSession, user: User) -> frozenset[uuid.UUID] | None:
    """None for an admin; the authority's assigned zone ids otherwise."""
    if user.role == "ADMIN":
        return None

    profile_id = await db.scalar(select(AuthorityUser.id).where(AuthorityUser.user_id == user.id))
    if profile_id is None:
        logger.warning("Analytics refused: AUTHORITY user_id=%s has no authority_users profile", user.id)
        raise WeftException(
            403,
            "AUTHORITY_PROFILE_MISSING",
            "This account has no authority profile, so it has no jurisdiction to report on.",
        )

    zone_ids = await db.scalars(select(AuthorityZone.zone_id).where(AuthorityZone.authority_user_id == profile_id))
    return frozenset(zone_ids.all())


def _as_utc(moment: datetime | None) -> datetime | None:
    """Read a timezone-less query parameter as UTC, so it names one instant."""
    if moment is not None and moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment


def _scoped(stmt: Select, query: AnalyticsQuery, statuses: Sequence[str] | None = None) -> Select:
    """Apply the caller's filters and their jurisdiction to a SELECT over `issues`."""
    stmt = _apply_filters(
        stmt,
        IssueFilters(
            categories=list(query.categories) if query.categories else None,
            statuses=list(statuses) if statuses else None,
            zone_id=query.zone_id,
            department_id=query.department_id,
            from_date=query.from_date,
            to_date=query.to_date,
        ),
    )
    if query.scope_zone_ids is not None:
        # An empty scope renders as an always-false IN, which is the intent: an
        # authority with no zones has no issues. NULL zone_id never matches.
        stmt = stmt.where(Issue.zone_id.in_(sorted(query.scope_zone_ids)))
    return stmt


# ── Per-issue facts ─────────────────────────────────────────────────────


def _hours(interval):  # noqa: ANN001, ANN202 — SQLAlchemy interval in, float expression out
    """An SQL interval as fractional hours, typed double precision.

    `EXTRACT` returns `numeric` on PostgreSQL 14+; casting to float keeps `avg`
    and `percentile_cont` in one type and hands Python floats back.
    """
    return cast(func.extract("epoch", interval) / 3600.0, Float)


def _issue_facts(query: AnalyticsQuery, statuses: Sequence[str] | None = None) -> Select:
    """One row per in-scope issue, carrying every derived figure the endpoints need.

    Every endpoint except the heatmap aggregates over this as a subquery, so a
    figure is defined once and cannot mean different things on different pages.

    The audit log is read through one LATERAL subquery per issue, served by
    `idx_status_history_issue (issue_id, created_at DESC)`: the latest RESOLVED
    transition and the reopen count come out of the same handful of rows.
    """
    history = (
        select(
            func.max(IssueStatusHistory.created_at)
            .filter(IssueStatusHistory.new_status == "RESOLVED")
            .label("last_resolved_at"),
            # RESOLVED → IN_PROGRESS is the only legal move out of RESOLVED
            # (D-12), so every row leaving RESOLVED is a reopen.
            func.count().filter(IssueStatusHistory.previous_status == "RESOLVED").label("reopen_count"),
        )
        .where(IssueStatusHistory.issue_id == Issue.id)
        .lateral("history")
    )

    # Only a RESOLVED issue has a standing resolution: a reopened one's earlier
    # RESOLVED row is history, not an answer.
    resolved_at = case(
        (Issue.status == "RESOLVED", func.coalesce(history.c.last_resolved_at, Issue.resolved_at)),
        else_=None,
    )
    age_hours = _hours(func.now() - Issue.created_at)

    stmt = (
        select(
            Issue.id.label("id"),
            Issue.issue_number.label("issue_number"),
            Issue.category.label("category"),
            Issue.status.label("status"),
            Issue.description.label("description"),
            Issue.address_text.label("address_text"),
            Issue.latitude.label("latitude"),
            Issue.longitude.label("longitude"),
            Issue.zone_id.label("zone_id"),
            Zone.name.label("zone_name"),
            Issue.department_id.label("department_id"),
            Department.name.label("department_name"),
            Department.sla_hours.label("sla_hours"),
            Issue.upvote_count.label("upvote_count"),
            Issue.assigned_to_id.label("assigned_to_id"),
            AuthorityUser.employee_id.label("assigned_to_employee_id"),
            Issue.assigned_at.label("assigned_at"),
            Issue.created_at.label("created_at"),
            Issue.updated_at.label("updated_at"),
            resolved_at.label("resolved_at"),
            _hours(resolved_at - Issue.created_at).label("resolution_hours"),
            Issue.resolution_note.label("resolution_note"),
            history.c.reopen_count.label("reopen_count"),
            age_hours.label("age_hours"),
            and_(
                Issue.status.in_(OPEN_STATUSES),
                Department.sla_hours.is_not(None),
                age_hours > Department.sla_hours,
            ).label("sla_breached"),
        )
        .select_from(Issue)
        .outerjoin(Department, Department.id == Issue.department_id)
        .outerjoin(Zone, Zone.id == Issue.zone_id)
        .outerjoin(AuthorityUser, AuthorityUser.id == Issue.assigned_to_id)
        .outerjoin(history, true())
    )
    return _scoped(stmt, query, statuses)


def _round(value: float | Decimal | None) -> float | None:
    """Two decimal places, or None. Never turns a missing figure into 0."""
    return None if value is None else round(float(value), 2)


# ── Summary ─────────────────────────────────────────────────────────────


async def summary(db: AsyncSession, query: AnalyticsQuery) -> AnalyticsSummary:
    """Headline KPIs: one aggregate pass, plus the per-category counts."""
    facts = _issue_facts(query).subquery("facts")
    hours = facts.c.resolution_hours

    totals = (
        await db.execute(
            select(
                func.count().label("total"),
                func.count().filter(facts.c.status == "REPORTED").label("awaiting_triage"),
                func.count().filter(facts.c.status == "IN_PROGRESS").label("in_progress"),
                func.count().filter(facts.c.status == "RESOLVED").label("resolved"),
                func.count().filter(facts.c.status == "REJECTED").label("rejected"),
                func.count().filter(facts.c.reopen_count > 0).label("reopened"),
                func.count().filter(facts.c.sla_breached).label("sla_breaches"),
                func.avg(hours).label("avg_hours"),
                func.percentile_cont(0.5).within_group(hours).label("median_hours"),
            )
        )
    ).one()

    by_category = dict(
        (await db.execute(select(facts.c.category, func.count()).group_by(facts.c.category))).tuples().all()
    )

    return AnalyticsSummary(
        total_reported=totals.total,
        total_awaiting_triage=totals.awaiting_triage,
        total_in_progress=totals.in_progress,
        total_resolved=totals.resolved,
        total_rejected=totals.rejected,
        total_reopened=totals.reopened,
        sla_breach_count=totals.sla_breaches,
        avg_resolution_hours=_round(totals.avg_hours),
        median_resolution_hours=_round(totals.median_hours),
        category_breakdown=[CategoryCount(category=c, count=by_category.get(c.value, 0)) for c in IssueCategory],
        generated_at=datetime.now(UTC),
    )


# ── Heatmap ─────────────────────────────────────────────────────────────


async def heatmap(
    db: AsyncSession,
    query: AnalyticsQuery,
    *,
    statuses: Sequence[str] = OPEN_STATUSES,
    grid_size_deg: float = DEFAULT_HEATMAP_GRID_DEG,
) -> HeatmapResponse:
    """Issue density snapped to a square lat/lng grid, densest cells first.

    Each issue snaps to the nearest grid node — `round(coordinate / grid) *
    grid`, the same rounding `ST_SnapToGrid` performs, which is what TRD §9
    specifies. It is done on the `numeric` latitude/longitude columns rather
    than on the PostGIS point so the nodes come back as exact decimals
    (`12.97`, not `12.970000000000001`) and two issues in one cell can never
    land on float-distinct nodes.

    Grid aggregation was chosen over returning raw weighted points because it
    bounds the payload by area, not by issue count: a city's worth of reports
    is a few hundred cells however many issues it holds. Defaults to open
    issues only — a heatmap of resolved problems shows where work *was*.
    """
    grid = literal(Decimal(str(grid_size_deg)), Numeric)

    # Snap in an inner query and group in the outer one. Grouping directly by
    # `round(lat / :grid) * :grid` would bind the grid twice as two distinct
    # parameters, and PostgreSQL would then refuse to match the GROUP BY
    # expression to the SELECT one.
    cells = _scoped(
        select(
            (func.round(Issue.latitude / grid) * grid).label("lat"),
            (func.round(Issue.longitude / grid) * grid).label("lng"),
            Issue.upvote_count.label("upvotes"),
        ),
        query,
        statuses,
    ).subquery("cells")

    issue_count = func.count().label("issue_count")
    rows = (
        await db.execute(
            select(cells.c.lat, cells.c.lng, issue_count, func.sum(cells.c.upvotes).label("total_upvotes"))
            .group_by(cells.c.lat, cells.c.lng)
            .order_by(issue_count.desc(), cells.c.lat, cells.c.lng)
            .limit(MAX_HEATMAP_POINTS + 1)
        )
    ).all()

    return HeatmapResponse(
        grid_size_deg=grid_size_deg,
        points=[
            HeatmapPoint(
                lat=float(r.lat),
                lng=float(r.lng),
                issue_count=r.issue_count,
                total_upvotes=int(r.total_upvotes or 0),
            )
            for r in rows[:MAX_HEATMAP_POINTS]
        ],
        truncated=len(rows) > MAX_HEATMAP_POINTS,
    )


# ── Resolution times ────────────────────────────────────────────────────


def _stats_columns(hours):  # noqa: ANN001, ANN202 — SQLAlchemy column in, column list out
    return (
        func.count().label("resolved_count"),
        func.avg(hours).label("avg_hours"),
        func.percentile_cont(0.5).within_group(hours).label("median_hours"),
    )


def _stats(row: Any) -> dict[str, Any]:
    return {
        "resolved_count": row.resolved_count,
        "avg_hours": _round(row.avg_hours),
        "median_hours": _round(row.median_hours),
    }


async def resolution_times(
    db: AsyncSession,
    query: AnalyticsQuery,
    *,
    group_by: ResolutionGroupBy = ResolutionGroupBy.CATEGORY,
    interval: TrendInterval = TrendInterval.WEEK,
) -> ResolutionTimesResponse:
    """Resolution times overall, per group, and as a trend.

    Trend buckets are the UTC day/week/month in which each issue's *standing*
    resolution was recorded — "how fast were things being closed in week N" —
    while the date filter still selects issues by report date, as everywhere
    else. Weeks start on Monday (`date_trunc('week')`).
    """
    facts = _issue_facts(query, statuses=["RESOLVED"]).subquery("facts")
    hours = facts.c.resolution_hours

    overall = (await db.execute(select(*_stats_columns(hours)))).one()

    key, label = {
        ResolutionGroupBy.CATEGORY: (facts.c.category, facts.c.category),
        ResolutionGroupBy.DEPARTMENT: (facts.c.department_id, facts.c.department_name),
        ResolutionGroupBy.ZONE: (facts.c.zone_id, facts.c.zone_name),
    }[group_by]
    group_rows = (
        await db.execute(
            select(key.label("key"), label.label("label"), *_stats_columns(hours))
            .group_by(key, label)
            .order_by(label.asc().nulls_last(), key)
        )
    ).all()

    # Bucket in an inner query for the same reason `heatmap` snaps in one: the
    # interval is a bound parameter, and a parameterised GROUP BY expression is
    # not matched to the identical SELECT expression.
    periods = select(
        cast(func.date_trunc(interval.value, func.timezone("UTC", facts.c.resolved_at)), Date).label("period_start"),
        hours.label("hours"),
    ).subquery("periods")
    trend_rows = (
        await db.execute(
            select(periods.c.period_start, *_stats_columns(periods.c.hours))
            .group_by(periods.c.period_start)
            .order_by(periods.c.period_start.desc())
            .limit(MAX_TREND_POINTS + 1)
        )
    ).all()

    return ResolutionTimesResponse(
        group_by=group_by,
        interval=interval,
        overall=ResolutionTimeStats(**_stats(overall)),
        groups=[
            ResolutionTimeGroup(
                key=None if r.key is None else str(r.key),
                label=UNASSIGNED_LABEL if r.label is None else str(r.label),
                **_stats(r),
            )
            for r in group_rows
        ],
        # Newest buckets were fetched first so the cap drops the oldest; the
        # response is oldest first, as a chart axis reads.
        trend=[
            ResolutionTrendPoint(period_start=r.period_start, **_stats(r))
            for r in reversed(trend_rows[:MAX_TREND_POINTS])
        ],
        trend_truncated=len(trend_rows) > MAX_TREND_POINTS,
    )


# ── SLA breaches ────────────────────────────────────────────────────────


async def sla_breaches(
    db: AsyncSession,
    query: AnalyticsQuery,
    *,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[SlaBreach], int]:
    """One page of open issues past their department SLA, most overdue first."""
    facts = _issue_facts(query, statuses=OPEN_STATUSES).subquery("facts")
    hours_overdue = (facts.c.age_hours - facts.c.sla_hours).label("hours_overdue")
    base = select(facts, hours_overdue).where(facts.c.sla_breached)

    total = await db.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = (
        await db.execute(
            base.order_by((facts.c.age_hours - facts.c.sla_hours).desc(), facts.c.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()

    return [
        SlaBreach(
            id=r.id,
            issue_number=r.issue_number,
            category=r.category,
            status=r.status,
            address_text=r.address_text,
            zone_id=r.zone_id,
            zone_name=r.zone_name,
            department_id=r.department_id,
            department_name=r.department_name,
            assigned_to_id=r.assigned_to_id,
            upvote_count=r.upvote_count,
            sla_hours=r.sla_hours,
            age_hours=round(r.age_hours, 2),
            hours_overdue=round(r.hours_overdue, 2),
            created_at=r.created_at,
        )
        for r in rows
    ], total


# ── CSV export ──────────────────────────────────────────────────────────

# One row per issue, every field (PRD US-009). `reporter_id` is deliberately
# absent: anonymity is a product promise, and an export is the easiest place for
# it to leak — the same reason no issue response model carries it.
EXPORT_COLUMNS: tuple[str, ...] = (
    "issue_number",
    "issue_id",
    "category",
    "status",
    "description",
    "address_text",
    "latitude",
    "longitude",
    "zone_id",
    "zone_name",
    "department_id",
    "department_name",
    "upvote_count",
    "assigned_to_id",
    "assigned_to_employee_id",
    "assigned_at",
    "created_at",
    "updated_at",
    "resolved_at",
    "resolution_hours",
    "resolution_note",
    "reopen_count",
    "sla_hours",
    "age_hours",
    "sla_breached",
)

# Characters that make Excel, LibreOffice or Google Sheets treat a cell as a
# formula (OWASP "CSV Injection"). Descriptions and addresses are citizen text.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

# Rows per chunk handed to the response stream.
_EXPORT_CHUNK_ROWS = 500


class ExportTooLargeError(BadRequestError):
    """More issues match than one export may carry."""

    def __init__(self, limit: int) -> None:
        super().__init__(
            code="EXPORT_TOO_LARGE",
            message=f"The export would exceed {limit} rows. Narrow the date range or add filters.",
            details={"max_rows": limit},
        )


def neutralise_formula(value: str) -> str:
    """Make a text cell inert in spreadsheet software.

    A cell starting with a formula trigger gets a leading apostrophe, which
    every major spreadsheet treats as "this is text" and does not display. The
    check also looks past leading whitespace, because some importers trim it
    before deciding whether a cell is a formula.

    Applied to every *text* value in the export. Numbers this module formats
    itself (coordinates, hours) are never passed through it, so a negative
    longitude stays a number rather than becoming `'-73.98`.
    """
    stripped = value.lstrip(" \t\r\n")
    if value.startswith(_FORMULA_PREFIXES) or stripped.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def _csv_cell(value: Any) -> str:
    """One export value as CSV text. Only strings can carry a formula."""
    if value is None:
        return ""
    if isinstance(value, bool):  # before int: bool is an int subclass
        return "true" if value else "false"
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, float):
        return f"{value:.2f}"
    if isinstance(value, Decimal | int | uuid.UUID | date):
        return str(value)
    return neutralise_formula(str(value))


async def export_rows(db: AsyncSession, query: AnalyticsQuery) -> list[tuple[Any, ...]]:
    """Every in-scope issue as a tuple in `EXPORT_COLUMNS` order, oldest first.

    Raises:
        ExportTooLargeError: more than `EXPORT_MAX_ROWS` issues match.
    """
    facts = _issue_facts(query).subquery("facts")
    columns = [(facts.c.id if name == "issue_id" else facts.c[name]).label(name) for name in EXPORT_COLUMNS]
    stmt = select(*columns).order_by(facts.c.created_at, facts.c.id).limit(EXPORT_MAX_ROWS + 1)

    rows = (await db.execute(stmt)).all()
    if len(rows) > EXPORT_MAX_ROWS:
        raise ExportTooLargeError(EXPORT_MAX_ROWS)
    return [tuple(row) for row in rows]


def render_csv(rows: Sequence[tuple[Any, ...]]) -> Iterator[str]:
    """Stream a header plus `rows` as RFC 4180 CSV, in chunks.

    Starts with a UTF-8 byte-order mark: descriptions are written in six
    languages, and Excel — what the PRD's authority persona uses — reads a
    BOM-less CSV as the system code page and mangles every non-Latin script.
    Python's `utf-8-sig` and most other parsers skip the BOM.

    The stdlib `csv` writer does the quoting, so embedded commas, quotes and
    newlines in citizen text cannot break the row structure.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")

    yield "﻿"
    writer.writerow(EXPORT_COLUMNS)
    for index, row in enumerate(rows, start=1):
        writer.writerow([_csv_cell(v) for v in row])
        if index % _EXPORT_CHUNK_ROWS == 0:
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)
    yield buffer.getvalue()


def export_filename(now: datetime | None = None) -> str:
    """`weft-issues-20261009T143000Z.csv` — sortable, and safe in a header."""
    return f"weft-issues-{(now or datetime.now(UTC)):%Y%m%dT%H%M%SZ}.csv"
