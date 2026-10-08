"""Admin schemas — authority accounts, departments, zones, system stats.

Derived from the OpenAPI contract (`openapi.yaml`) component schemas
`CreateAuthorityRequest`, `AuthorityUser`, `PaginatedAuthorityUserResponse`,
`Department`, `DepartmentList`, `CreateDepartmentRequest`,
`UpdateDepartmentRequest`, `Zone`, `ZoneList`, `CreateZoneRequest`,
`GeoJSONPolygon` and `SystemStats`. Every one is pinned against the contract
by `tests/unit/test_contract_drift.py`.

Two rules shape the response models here:

* **No credential material, ever.** `AuthorityUser` is built from an explicit
  column list in `admin_service`, not from the ORM row, so `password_hash`,
  OAuth subjects and refresh tokens are never even read — a field added to the
  `users` table later cannot leak through this model by accident.
* **The initial password is admin-supplied** and must pass the same policy as
  citizen registration (`PASSWORD_MIN_LENGTH`, imported rather than restated
  so the two cannot drift). The TRD describes a system-generated password
  delivered by SES; there is no email infrastructure, and a generated password
  could only travel back in this API's response body. See the module docstring
  of `app.services.admin_service`.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from app.schemas.auth import PASSWORD_MIN_LENGTH, EmailAddress
from app.schemas.issue import IssueCategory
from app.schemas.user import UserRole

# Column widths from migrations 003-006. Enforced here so an over-long value is
# a 422 at the boundary rather than a DataError (500) from PostgreSQL.
NAME_MAX_LENGTH = 100
EMPLOYEE_ID_MAX_LENGTH = 50
DESIGNATION_MAX_LENGTH = 100
DEPARTMENT_CODE_MAX_LENGTH = 20
DEPARTMENT_CODE_PATTERN = r"^[A-Z0-9_]+$"

# Upper bounds exist because the columns are INTEGER: an unbounded value above
# 2^31 would be a 500 from the driver, not a validation error. One year is the
# longest SLA any department could meaningfully promise.
SLA_HOURS_MAX = 8_760
UPVOTE_ALERT_THRESHOLD_MAX = 1_000_000

# Hard ceiling on the vertex count of a submitted zone boundary. A municipal
# ward needs tens to a few hundred vertices; this bounds the JSON parse, the
# Python checks and the `ST_IsValid` call against a pathological payload.
MAX_BOUNDARY_POSITIONS = 10_000

TrimmedName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=NAME_MAX_LENGTH)]


# ── Enums ───────────────────────────────────────────────────────────────


class GeoJSONGeometryType(StrEnum):
    """GeoJSON geometry types accepted for a zone boundary.

    Only `Polygon`: `zones.boundary` is `GEOMETRY(POLYGON, 4326)`, so a
    MultiPolygon would be refused by the column anyway. A ward split by a river
    is two zones.
    """

    POLYGON = "Polygon"


# ── Authority accounts ──────────────────────────────────────────────────


class CreateAuthorityRequest(BaseModel):
    """Provision an authority account.

    Mirrors `components.schemas.CreateAuthorityRequest`. The account is always
    created with role `AUTHORITY`; ADMIN accounts are not provisioned through
    the API.
    """

    email: EmailAddress = Field(
        json_schema_extra={"format": "email"},
        description="Login email; must be unique across all accounts",
        examples=["officer@municipality.gov.in"],
    )
    name: TrimmedName = Field(description="Display name", examples=["Ravi Kumar"])
    password: str = Field(
        min_length=PASSWORD_MIN_LENGTH,
        description=f"Initial password, minimum {PASSWORD_MIN_LENGTH} characters (the citizen registration "
        "policy); bcrypt-hashed before storage and never returned",
    )
    employee_id: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=EMPLOYEE_ID_MAX_LENGTH)
    ] = Field(description="Municipality employee ID; must be unique", examples=["EMP-2026-001"])
    department_id: UUID = Field(description="Department the account belongs to; must exist and be active")
    designation: Annotated[str, StringConstraints(strip_whitespace=True, max_length=DESIGNATION_MAX_LENGTH)] | None = (
        Field(default=None, description="Job title", examples=["Field Supervisor"])
    )
    zone_ids: list[UUID] = Field(
        default_factory=list,
        description="Zones the account is responsible for; every id must exist. Duplicates are ignored.",
    )


class AuthorityUserOut(BaseModel):
    """One authority account, as an admin sees it.

    Mirrors `components.schemas.AuthorityUser`. Two identifiers, deliberately:
    `id` is the `authority_users` row — what `PATCH /issues/{id}/assign` takes
    as `assigned_to_id` — and `user_id` is the `users` row, which is what
    deactivation and login act on.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(description="Authority profile id; the value `assigned_to_id` expects")
    user_id: UUID = Field(description="User account id; the value the deactivate endpoint expects")
    email: str = Field(description="Login email")
    name: str | None = Field(default=None, description="Display name")
    role: UserRole = Field(description="AUTHORITY, or ADMIN for an admin with an authority profile")
    is_active: bool = Field(description="False once deactivated; a deactivated account cannot log in")
    employee_id: str = Field(description="Municipality employee ID")
    department_id: UUID = Field(description="Department the account belongs to")
    department_name: str = Field(description="Human-readable department name")
    designation: str | None = Field(default=None, description="Job title")
    is_dept_admin: bool = Field(description="Whether this account administers its own department")
    zone_ids: list[UUID] = Field(description="Zones the account is responsible for")
    created_at: datetime = Field(description="ISO 8601 provisioning timestamp")


# ── Departments ─────────────────────────────────────────────────────────


class CreateDepartmentRequest(BaseModel):
    """Create a department.

    Mirrors `components.schemas.CreateDepartmentRequest`. Category routing is
    deliberately **not** settable here: adding a category mapping changes where
    existing categories route (D-10 orders by `departments.code`), which is a
    routing decision, not a side effect of creating a department.
    """

    name: TrimmedName = Field(
        description="Human-readable name; unique, compared case-insensitively",
        examples=["Storm Water Drains"],
    )
    code: str = Field(
        min_length=1,
        max_length=DEPARTMENT_CODE_MAX_LENGTH,
        pattern=DEPARTMENT_CODE_PATTERN,
        description="Short unique code; uppercased before validation (A-Z, 0-9, underscore)",
        examples=["SWD"],
    )
    description: str | None = Field(default=None, description="Free-text description")
    contact_email: EmailAddress | None = Field(
        default=None,
        json_schema_extra={"format": "email"},
        description="Department contact address",
    )
    sla_hours: int = Field(default=72, ge=1, le=SLA_HOURS_MAX, description="Resolution SLA in hours")
    upvote_alert_threshold: int = Field(
        default=10,
        ge=1,
        le=UPVOTE_ALERT_THRESHOLD_MAX,
        description="Upvote count at which an issue.high_upvote_alert is raised",
    )

    @field_validator("code", mode="before")
    @classmethod
    def _normalise_code(cls, value: object) -> object:
        # A `mode="before"` validator rather than `StringConstraints(to_upper=…)`:
        # whether a pattern is checked before or after that transform differs
        # between Pydantic minor versions, and "pwd" must be accepted as "PWD".
        return value.strip().upper() if isinstance(value, str) else value


class UpdateDepartmentRequest(BaseModel):
    """Partial update of a department's operational thresholds (task 4.15a, D-1).

    Mirrors `components.schemas.UpdateDepartmentRequest`. Omitted fields are
    left unchanged. An explicit `null` is refused rather than read as "no
    change" — both columns are NOT NULL, and a client that sends null almost
    certainly meant something.
    """

    sla_hours: int | None = Field(default=None, ge=1, le=SLA_HOURS_MAX, description="Resolution SLA in hours")
    upvote_alert_threshold: int | None = Field(
        default=None,
        ge=1,
        le=UPVOTE_ALERT_THRESHOLD_MAX,
        description="Upvote count at which an issue.high_upvote_alert is raised",
    )

    @model_validator(mode="after")
    def _reject_nulls_and_empty(self) -> UpdateDepartmentRequest:
        if not self.model_fields_set:
            raise ValueError("Provide at least one of sla_hours, upvote_alert_threshold")
        nulls = sorted(name for name in self.model_fields_set if getattr(self, name) is None)
        if nulls:
            raise ValueError(f"Field(s) may not be null: {', '.join(nulls)}")
        return self


class DepartmentOut(BaseModel):
    """One department. Mirrors `components.schemas.Department`."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(description="Department id")
    name: str = Field(description="Human-readable name")
    code: str = Field(description="Short unique code")
    description: str | None = Field(default=None, description="Free-text description")
    contact_email: str | None = Field(default=None, description="Department contact address")
    sla_hours: int = Field(description="Resolution SLA in hours")
    upvote_alert_threshold: int = Field(description="Upvote count at which an issue.high_upvote_alert is raised")
    is_active: bool = Field(description="Inactive departments receive no routed issues")
    categories: list[IssueCategory] = Field(description="Issue categories routed to this department")
    created_at: datetime = Field(description="ISO 8601 creation timestamp")


class DepartmentListResponse(BaseModel):
    """Every department, ordered by code. Mirrors `components.schemas.DepartmentList`."""

    items: list[DepartmentOut]


# ── Zones ───────────────────────────────────────────────────────────────


class GeoJSONPolygon(BaseModel):
    """A GeoJSON Polygon in WGS84 (RFC 7946), positions as `[longitude, latitude]`.

    Mirrors `components.schemas.GeoJSONPolygon`. Only the *shape* is checked
    here; closure, ring length, coordinate ranges and topological validity are
    checked by `admin_service`, so that every geometric problem surfaces as the
    same `400 INVALID_GEOMETRY` with a reason, rather than half of them as a
    422 and half as a 400.
    """

    type: GeoJSONGeometryType = Field(description="Always 'Polygon'")
    coordinates: list[list[list[float]]] = Field(
        description="Linear rings: the first is the exterior, any further ones are holes. "
        "Each ring is closed (first position equals last) and has at least 4 positions.",
    )


class CreateZoneRequest(BaseModel):
    """Create a geographic zone. Mirrors `components.schemas.CreateZoneRequest`."""

    name: TrimmedName = Field(description="Zone name", examples=["Ward 12 — Shivajinagar"])
    department_id: UUID | None = Field(default=None, description="Owning department, if any; must exist")
    boundary: GeoJSONPolygon = Field(description="Zone boundary, SRID 4326")


class ZoneOut(BaseModel):
    """One zone, boundary included. Mirrors `components.schemas.Zone`."""

    id: UUID = Field(description="Zone id")
    name: str = Field(description="Zone name")
    department_id: UUID | None = Field(default=None, description="Owning department, if any")
    is_active: bool = Field(description="Inactive zones are skipped by issue auto-assignment")
    boundary: GeoJSONPolygon = Field(description="Zone boundary, SRID 4326")
    created_at: datetime = Field(description="ISO 8601 creation timestamp")


class ZoneListResponse(BaseModel):
    """Every zone, ordered by name. Mirrors `components.schemas.ZoneList`."""

    items: list[ZoneOut]


# ── System stats ────────────────────────────────────────────────────────


class SystemStats(BaseModel):
    """System-wide counts for the admin overview. Mirrors `components.schemas.SystemStats`.

    Plain row counts taken at request time, not analytics: no date windows, no
    per-department breakdown. Those belong to `/analytics/*`.
    """

    total_users: int = Field(description="Every account, all roles, active or not")
    total_issues: int = Field(description="Every issue ever submitted, any status")
    total_resolved: int = Field(description="Issues currently in RESOLVED")
    active_authority_users: int = Field(description="Authority profiles whose account is active")
    departments: int = Field(description="Active departments")
    zones: int = Field(description="Active zones")
