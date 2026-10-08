"""Admin router — authority accounts, departments, zones, system stats.

Every route here requires the ADMIN role: an unauthenticated call is a 401, any
other role a 403. The guard is a per-handler dependency rather than a
router-level one because several handlers need the acting admin themselves —
to refuse self-deactivation and to record who provisioned an account.

Every handler is a translation layer: check the role, delegate to
`app.services.admin_service`, shape the response. No query and no business rule
lives here, and nothing logs an email, a password or a token.

The PRD's *department* admin (US-011: "AUTHORITY role can only manage their own
department", `authority_users.is_dept_admin`) is not built. These are
system-admin endpoints only.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.core.permissions import require_admin
from app.dependencies import DBSession
from app.models.user import User
from app.schemas.admin import (
    AuthorityUserOut,
    CreateAuthorityRequest,
    CreateDepartmentRequest,
    CreateZoneRequest,
    DepartmentListResponse,
    DepartmentOut,
    SystemStats,
    UpdateDepartmentRequest,
    ZoneListResponse,
    ZoneOut,
)
from app.schemas.common import PaginatedResponse
from app.services import admin_service

logger = logging.getLogger(__name__)

router = APIRouter()

AdminUser = Annotated[User, Depends(require_admin)]


def _to_department_out(department) -> DepartmentOut:  # noqa: ANN001 — Department ORM row
    return DepartmentOut(
        id=department.id,
        name=department.name,
        code=department.code,
        description=department.description,
        contact_email=department.contact_email,
        sla_hours=department.sla_hours,
        upvote_alert_threshold=department.upvote_alert_threshold,
        is_active=department.is_active,
        categories=admin_service.department_categories(department),
        created_at=department.created_at,
    )


# ── GET /admin/authority-users ──────────────────────────────────────────


@router.get(
    "/authority-users",
    response_model=PaginatedResponse[AuthorityUserOut],
    summary="List authority accounts",
)
async def list_authority_users(
    db: DBSession,
    current_user: AdminUser,
    department_id: uuid.UUID | None = None,
    is_active: bool | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> PaginatedResponse[AuthorityUserOut]:
    """Paginated directory of authority accounts, ordered by name.

    Backs the admin team page and the staff picker for issue assignment: each
    item's `id` is what `PATCH /issues/{id}/assign` takes.
    """
    rows, total = await admin_service.list_authority_users(
        db,
        filters=admin_service.AuthorityUserFilters(department_id=department_id, is_active=is_active),
        page=page,
        page_size=page_size,
    )
    return PaginatedResponse.create(
        items=[AuthorityUserOut(**row) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


# ── POST /admin/authority-users ─────────────────────────────────────────


@router.post(
    "/authority-users",
    response_model=AuthorityUserOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create an authority account",
)
async def create_authority_user(
    db: DBSession,
    payload: CreateAuthorityRequest,
    current_user: AdminUser,
) -> AuthorityUserOut:
    """Provision an AUTHORITY account with an admin-supplied initial password.

    The password is never echoed back. The admin hands it to the new user out
    of band; there is no email delivery (see `admin_service`).
    """
    row = await admin_service.create_authority_user(db, payload, actor=current_user)
    return AuthorityUserOut(**row)


# ── PATCH /admin/authority-users/{user_id}/deactivate ───────────────────


@router.patch(
    "/authority-users/{user_id}/deactivate",
    response_model=AuthorityUserOut,
    summary="Deactivate an authority account",
)
async def deactivate_authority_user(
    db: DBSession,
    user_id: uuid.UUID,
    current_user: AdminUser,
) -> AuthorityUserOut:
    """Deactivate (never delete) an account and end every session it has.

    `user_id` is the account's *user* id (`AuthorityUser.user_id`), not its
    profile id. Idempotent; refuses to deactivate the calling admin.
    """
    row = await admin_service.deactivate_authority_user(db, user_id=user_id, actor=current_user)
    return AuthorityUserOut(**row)


# ── GET /admin/departments ──────────────────────────────────────────────


@router.get("/departments", response_model=DepartmentListResponse, summary="List departments")
async def list_departments(db: DBSession, current_user: AdminUser) -> DepartmentListResponse:
    """Every department, active or not, with SLA, alert threshold and routed categories."""
    departments = await admin_service.list_departments(db)
    return DepartmentListResponse(items=[_to_department_out(d) for d in departments])


# ── POST /admin/departments ─────────────────────────────────────────────


@router.post(
    "/departments",
    response_model=DepartmentOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a department",
)
async def create_department(
    db: DBSession,
    payload: CreateDepartmentRequest,
    current_user: AdminUser,
) -> DepartmentOut:
    """Create a department. It routes no categories until a mapping is added."""
    department = await admin_service.create_department(db, payload)
    return _to_department_out(department)


# ── PATCH /admin/departments/{department_id} ────────────────────────────


@router.patch(
    "/departments/{department_id}",
    response_model=DepartmentOut,
    summary="Update a department's SLA and upvote alert threshold",
)
async def update_department(
    db: DBSession,
    department_id: uuid.UUID,
    payload: UpdateDepartmentRequest,
    current_user: AdminUser,
) -> DepartmentOut:
    """Edit `sla_hours` and/or `upvote_alert_threshold`, each at least 1 (task 4.15a, D-1)."""
    department = await admin_service.update_department(db, department_id=department_id, payload=payload)
    return _to_department_out(department)


# ── GET /admin/zones ────────────────────────────────────────────────────


@router.get("/zones", response_model=ZoneListResponse, summary="List zones")
async def list_zones(db: DBSession, current_user: AdminUser) -> ZoneListResponse:
    """Every zone with its GeoJSON boundary — for the zone picker and the admin map."""
    zones = await admin_service.list_zones(db)
    return ZoneListResponse(items=[ZoneOut(**z) for z in zones])


# ── POST /admin/zones ───────────────────────────────────────────────────


@router.post(
    "/zones",
    response_model=ZoneOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a geographic zone",
)
async def create_zone(db: DBSession, payload: CreateZoneRequest, current_user: AdminUser) -> ZoneOut:
    """Create a zone from a GeoJSON Polygon (SRID 4326).

    A malformed, out-of-range or self-intersecting boundary is a
    `400 INVALID_GEOMETRY` carrying the reason.
    """
    zone = await admin_service.create_zone(db, payload)
    return ZoneOut(**zone)


# ── GET /admin/system/stats ─────────────────────────────────────────────


@router.get("/system/stats", response_model=SystemStats, summary="System-wide usage statistics")
async def system_stats(db: DBSession, current_user: AdminUser) -> SystemStats:
    """Live row counts: users, issues, resolved issues, active staff, departments, zones."""
    return SystemStats(**await admin_service.system_stats(db))
