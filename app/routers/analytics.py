"""Analytics router — authority dashboard KPIs, heatmap, resolution times,
SLA breaches and CSV export.

Every handler is a translation layer over `app.services.analytics_service`,
which documents how each figure is defined. Everything is computed live from
`issues` and `issue_status_history`; there is no snapshot table yet (task 4.12).

**Staff only, and scoped.** Every route requires an AUTHORITY or ADMIN token.
An admin sees every issue; an authority sees only issues in the zones they are
assigned (`authority_zones`). The scope is resolved once per request by the
`AnalyticsScope` dependency, so no handler can forget to apply it.

`format=pdf` on the export is answered with 501 `EXPORT_FORMAT_NOT_SUPPORTED`.
PDF generation is task 4.18 (Phase 4); a placeholder file would look like a
report while containing none.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import StreamingResponse

from app.core.exceptions import WeftException
from app.core.permissions import require_authority
from app.dependencies import DBSession
from app.models.user import User
from app.schemas.analytics import (
    AnalyticsSummary,
    ExportFormat,
    HeatmapResponse,
    ResolutionGroupBy,
    ResolutionTimesResponse,
    SlaBreach,
    TrendInterval,
)
from app.schemas.common import PaginatedResponse
from app.schemas.issue import IssueCategory, IssueStatus
from app.services import analytics_service
from app.services.analytics_service import AnalyticsQuery

logger = logging.getLogger(__name__)

router = APIRouter()

StaffUser = Annotated[User, Depends(require_authority)]


async def _analytics_query(
    db: DBSession,
    current_user: StaffUser,
    from_date: Annotated[datetime | None, Query(description="Reported at or after (inclusive)")] = None,
    to_date: Annotated[datetime | None, Query(description="Reported at or before (inclusive)")] = None,
    department_id: uuid.UUID | None = None,
    zone_id: uuid.UUID | None = None,
    category: Annotated[list[IssueCategory] | None, Query(description="Repeat the key for several")] = None,
) -> AnalyticsQuery:
    """The filters every analytics route shares, resolved against the caller's scope."""
    return await analytics_service.build_query(
        db,
        user=current_user,
        from_date=from_date,
        to_date=to_date,
        department_id=department_id,
        zone_id=zone_id,
        categories=[c.value for c in category] if category else None,
    )


AnalyticsScope = Annotated[AnalyticsQuery, Depends(_analytics_query)]


# ── GET /analytics/summary ──────────────────────────────────────────────


@router.get("/summary", response_model=AnalyticsSummary, summary="KPI summary")
async def analytics_summary(db: DBSession, query: AnalyticsScope) -> AnalyticsSummary:
    """Totals by status, reopenings, SLA breaches, resolution time and category mix."""
    return await analytics_service.summary(db, query)


# ── GET /analytics/heatmap ──────────────────────────────────────────────


@router.get("/heatmap", response_model=HeatmapResponse, summary="Grid-aggregated issue density")
async def analytics_heatmap(
    db: DBSession,
    query: AnalyticsScope,
    issue_status: Annotated[
        list[IssueStatus] | None,
        Query(alias="status", description="Defaults to REPORTED and IN_PROGRESS"),
    ] = None,
    grid: Annotated[
        float,
        Query(
            ge=analytics_service.MIN_HEATMAP_GRID_DEG,
            le=analytics_service.MAX_HEATMAP_GRID_DEG,
            description="Grid cell edge in degrees",
        ),
    ] = analytics_service.DEFAULT_HEATMAP_GRID_DEG,
) -> HeatmapResponse:
    """Issue counts and upvote totals per grid cell, for Leaflet.heat."""
    return await analytics_service.heatmap(
        db,
        query,
        statuses=[s.value for s in issue_status] if issue_status else analytics_service.OPEN_STATUSES,
        grid_size_deg=grid,
    )


# ── GET /analytics/resolution-times ─────────────────────────────────────


@router.get("/resolution-times", response_model=ResolutionTimesResponse, summary="Resolution times")
async def analytics_resolution_times(
    db: DBSession,
    query: AnalyticsScope,
    group_by: ResolutionGroupBy = ResolutionGroupBy.CATEGORY,
    interval: TrendInterval = TrendInterval.WEEK,
) -> ResolutionTimesResponse:
    """Report-to-resolution hours overall, per category/department/zone, and over time."""
    return await analytics_service.resolution_times(db, query, group_by=group_by, interval=interval)


# ── GET /analytics/sla-breaches ─────────────────────────────────────────


@router.get("/sla-breaches", response_model=PaginatedResponse[SlaBreach], summary="Open issues past their SLA")
async def analytics_sla_breaches(
    db: DBSession,
    query: AnalyticsScope,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> PaginatedResponse[SlaBreach]:
    """Open issues older than their department's `sla_hours`, most overdue first."""
    items, total = await analytics_service.sla_breaches(db, query, page=page, page_size=page_size)
    return PaginatedResponse.create(items=items, total=total, page=page, page_size=page_size)


# ── GET /analytics/export ───────────────────────────────────────────────


@router.get(
    "/export",
    # The body is a file, not JSON. `response_model=None` stops FastAPI deriving
    # a JSON schema from the StreamingResponse annotation.
    response_model=None,
    response_class=StreamingResponse,
    summary="Export issues as CSV",
    responses={status.HTTP_200_OK: {"content": {"text/csv": {}}, "description": "CSV file download"}},
)
async def analytics_export(
    db: DBSession,
    query: AnalyticsScope,
    export_format: Annotated[ExportFormat, Query(alias="format")] = ExportFormat.CSV,
) -> StreamingResponse:
    """One row per in-scope issue, every field, as a CSV download.

    Text cells that a spreadsheet would execute as a formula are neutralised —
    descriptions and addresses are citizen-submitted. See
    `analytics_service.neutralise_formula`.
    """
    if export_format is ExportFormat.PDF:
        raise WeftException(
            status.HTTP_501_NOT_IMPLEMENTED,
            "EXPORT_FORMAT_NOT_SUPPORTED",
            "PDF export is not available yet. Use format=csv.",
            {"format": export_format.value, "supported": [ExportFormat.CSV.value]},
        )

    rows = await analytics_service.export_rows(db, query)
    logger.info("Analytics CSV export rows=%d scoped=%s", len(rows), query.scope_zone_ids is not None)
    return StreamingResponse(
        analytics_service.render_csv(rows),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{analytics_service.export_filename()}"'},
    )
