"""Analytics schemas — KPI summary, heatmap, resolution times, SLA breaches.

Derived from the `openapi.yaml` component schemas `AnalyticsSummary`,
`CategoryCount`, `HeatmapResponse`, `HeatmapPoint`, `ResolutionTimeStats`,
`ResolutionTimeGroup`, `ResolutionTrendPoint`, `ResolutionTimesResponse`,
`SlaBreach` and `PaginatedSlaBreachResponse`. `tests/unit/test_contract_drift.py`
pins every one of them.

**Every field on these response models is required, including the nullable
ones.** `avg_resolution_hours` is `float | None` with no default: it is always
present on the wire and is `null` when nothing in scope has been resolved. That
is a deliberate distinction from `0` — an average of zero hours is a figure, and
a dashboard would draw it as one. A client generated from the contract therefore
types these as `number | null`, never as optional.

All hour figures are wall-clock hours rounded to two decimal places. How each one
is measured is documented once, in `app.services.analytics_service`.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from app.schemas.issue import IssueCategory, IssueStatus

# ── Enums ───────────────────────────────────────────────────────────────


class ResolutionGroupBy(StrEnum):
    """Dimension `GET /analytics/resolution-times` breaks its figures down by."""

    CATEGORY = "category"
    DEPARTMENT = "department"
    ZONE = "zone"


class TrendInterval(StrEnum):
    """Bucket width of the resolution-time trend. Buckets are UTC calendar periods."""

    DAY = "day"
    WEEK = "week"
    MONTH = "month"


class ExportFormat(StrEnum):
    """Formats `GET /analytics/export` accepts.

    `pdf` is listed because the contract lists it (task 3.25), but it is not
    implemented: PDF generation is task 4.18 and the endpoint answers it with a
    501 `EXPORT_FORMAT_NOT_SUPPORTED` rather than a placeholder file.
    """

    CSV = "csv"
    PDF = "pdf"


# ── Summary ─────────────────────────────────────────────────────────────


class CategoryCount(BaseModel):
    """How many in-scope issues carry one category."""

    category: IssueCategory = Field(description="Issue category")
    count: int = Field(ge=0, description="Number of issues in this category")


class AnalyticsSummary(BaseModel):
    """Headline KPIs for the analytics page, computed live.

    `total_reported` counts every issue matching the filters, whatever its
    status, and always equals the sum of the four per-status totals.
    """

    total_reported: int = Field(ge=0, description="Every issue matching the filters, in any status")
    total_awaiting_triage: int = Field(ge=0, description="Issues still in REPORTED status")
    total_in_progress: int = Field(ge=0, description="Issues in IN_PROGRESS status (including reopened ones)")
    total_resolved: int = Field(ge=0, description="Issues currently RESOLVED")
    total_rejected: int = Field(ge=0, description="Issues currently REJECTED")
    total_reopened: int = Field(
        ge=0,
        description="Issues that were resolved and later reopened at least once, whatever their status now",
    )
    sla_breach_count: int = Field(
        ge=0,
        description="Open issues (not RESOLVED or REJECTED) older than their department's sla_hours",
    )
    avg_resolution_hours: float | None = Field(
        description="Mean hours from report to the resolution that currently stands; null when none resolved",
    )
    median_resolution_hours: float | None = Field(
        description="Median of the same measure; null when none resolved",
    )
    category_breakdown: list[CategoryCount] = Field(
        description="One entry per category, all seven always present, in contract enum order",
    )
    generated_at: datetime = Field(description="ISO 8601 instant the figures were computed")


# ── Heatmap ─────────────────────────────────────────────────────────────


class HeatmapPoint(BaseModel):
    """One grid cell holding at least one issue."""

    lat: float = Field(ge=-90, le=90, description="Latitude of the grid node the cell's issues snapped to")
    lng: float = Field(ge=-180, le=180, description="Longitude of the grid node the cell's issues snapped to")
    issue_count: int = Field(ge=1, description="Issues in this cell")
    total_upvotes: int = Field(ge=0, description="Sum of upvote_count over the cell's issues")


class HeatmapResponse(BaseModel):
    """Grid-aggregated issue density for Leaflet.heat.

    Bounded: at most `MAX_HEATMAP_POINTS` cells, densest first. `truncated` says
    whether any cells were dropped, so a client never mistakes a capped map for
    a complete one.
    """

    grid_size_deg: float = Field(gt=0, description="Edge length of a grid cell, in degrees")
    points: list[HeatmapPoint] = Field(description="Occupied cells, densest first")
    truncated: bool = Field(description="True when more cells existed than were returned")


# ── Resolution times ────────────────────────────────────────────────────


class ResolutionTimeStats(BaseModel):
    """Resolution-time figures over a set of currently-resolved issues."""

    resolved_count: int = Field(ge=0, description="Currently-resolved issues the figures are computed over")
    avg_hours: float | None = Field(description="Mean resolution hours; null when resolved_count is 0")
    median_hours: float | None = Field(description="Median resolution hours; null when resolved_count is 0")


class ResolutionTimeGroup(ResolutionTimeStats):
    """Resolution-time figures for one category, department or zone."""

    key: str | None = Field(
        description="Category value, or department/zone UUID; null for issues with no department/zone",
    )
    label: str = Field(description="Human-readable name: the category, or the department/zone name")


class ResolutionTrendPoint(ResolutionTimeStats):
    """Resolution-time figures for issues whose standing resolution fell in one period."""

    period_start: date = Field(description="First day (UTC) of the day/week/month bucket")


class ResolutionTimesResponse(BaseModel):
    """Resolution times broken down by a dimension, plus a trend over time."""

    group_by: ResolutionGroupBy = Field(description="Dimension the `groups` are split by")
    interval: TrendInterval = Field(description="Bucket width of `trend`")
    overall: ResolutionTimeStats = Field(description="Figures over every resolved issue in scope")
    groups: list[ResolutionTimeGroup] = Field(description="One entry per group with at least one resolved issue")
    trend: list[ResolutionTrendPoint] = Field(
        description="Buckets holding at least one resolution, oldest first; empty buckets are omitted",
    )
    trend_truncated: bool = Field(description="True when older buckets were dropped to bound the response")


# ── SLA breaches ────────────────────────────────────────────────────────


class SlaBreach(BaseModel):
    """An open issue that has outlived its department's SLA."""

    id: uuid.UUID = Field(description="Issue id")
    issue_number: str = Field(description="Human-readable reference", examples=["ISS-2026-K7QD3M8XPZ"])
    category: IssueCategory = Field(description="Reported category")
    status: IssueStatus = Field(description="Current status — always REPORTED or IN_PROGRESS here")
    address_text: str | None = Field(description="Address as submitted, if any")
    zone_id: uuid.UUID | None = Field(description="Zone of the report, if any")
    zone_name: str | None = Field(description="Name of that zone")
    department_id: uuid.UUID = Field(description="Department whose SLA was breached")
    department_name: str = Field(description="Name of that department")
    assigned_to_id: uuid.UUID | None = Field(description="`authority_users.id` currently responsible, if any")
    upvote_count: int = Field(ge=0, description="Community priority signal")
    sla_hours: int = Field(description="The department's SLA threshold, in hours")
    age_hours: float = Field(description="Hours since the issue was reported")
    hours_overdue: float = Field(gt=0, description="age_hours minus sla_hours")
    created_at: datetime = Field(description="ISO 8601 submission timestamp")
