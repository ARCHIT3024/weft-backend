"""Departments router — the read staff need, without admin rights.

`GET /departments` exists because the dashboard draws SLA countdowns and breach
badges from each department's `sla_hours`, and until now could only read it
from `GET /admin/departments`, which is ADMIN-only — so `weft-web` hard-coded
72 hours for everyone. Any AUTHORITY or ADMIN may read it; an unauthenticated
call is a 401, a citizen's a 403.

Not scoped by zone: departments are city-wide configuration, not issue data,
and an authority triaging an issue routed to another department still needs
that department's SLA to read it.

A translation layer over `admin_service.list_departments`, so the two lists can
never disagree about which departments exist or in what order. Writes stay
under `/admin/departments`.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.permissions import require_authority
from app.dependencies import DBSession
from app.models.user import User
from app.schemas.department import DepartmentSummary, DepartmentSummaryList
from app.services import admin_service

router = APIRouter()

StaffUser = Annotated[User, Depends(require_authority)]


# ── GET /departments ────────────────────────────────────────────────────


@router.get("", response_model=DepartmentSummaryList, summary="List departments (staff)")
async def list_departments(db: DBSession, current_user: StaffUser) -> DepartmentSummaryList:
    """Every department, active or not, ordered by code, with its SLA and alert threshold."""
    departments = await admin_service.list_departments(db)
    return DepartmentSummaryList(items=[DepartmentSummary.model_validate(d) for d in departments])
