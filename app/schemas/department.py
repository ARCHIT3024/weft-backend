"""Department read schemas for staff — `GET /departments`.

Derived from the OpenAPI contract (`openapi.yaml`) component schemas
`DepartmentSummary` and `DepartmentSummaryList`, and pinned against them by
`tests/unit/test_contract_drift.py`.

A deliberately smaller shape than the admin `Department` component
(`app.schemas.admin.DepartmentOut`): the dashboard needs each department's SLA
to draw breach countdowns, and its alert threshold to explain the upvote toast,
but not its contact address or category routing, which are admin concerns.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class DepartmentSummary(BaseModel):
    """One department as staff see it. Mirrors `components.schemas.DepartmentSummary`."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(description="Department id — the `department_id` on every issue")
    name: str = Field(description="Human-readable name")
    code: str = Field(description="Short unique code")
    sla_hours: int = Field(description="Resolution SLA in hours; an open issue older than this is in breach")
    upvote_alert_threshold: int = Field(description="Upvote count at which an issue.high_upvote_alert is raised")
    is_active: bool = Field(description="Inactive departments receive no newly routed issues")


class DepartmentSummaryList(BaseModel):
    """Every department, ordered by code. Mirrors `components.schemas.DepartmentSummaryList`."""

    items: list[DepartmentSummary]
