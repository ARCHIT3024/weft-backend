"""SQLAlchemy ORM model for the `issues` table.

The core table: one row per reported civic issue.  Carries the geospatial
point used by the map, the lifecycle status, the department/zone routing
result and the authority assignment.

MVP SCOPE — this table is a deliberate subset of
`05_weft_backend_schema.md` Section 3.7.  Columns that exist only to serve a
feature cut from the local MVP are omitted rather than left as dead NULLs:

* `ai_suggested_category`, `ai_confidence` — AI categorisation is cut.
* `phash`                                 — perceptual-hash dedupe is cut.
* `is_flagged`                            — image moderation is cut.
* `captured_at`                           — offline capture/queue is cut.

`upvote_count` is a denormalised counter owned by the database: the
`trg_upvote_count` trigger (migration 012) increments it on every `upvotes`
INSERT and decrements it on every DELETE.  **Application code never writes
it** — `issue_service` inserts and deletes `upvotes` rows and re-reads the
counter, which stays correct under concurrent votes in a way a Python
read-modify-write would not.

`location` is a STORED generated column derived from `longitude`/`latitude`,
so it can never drift from them and is never written by the application.  It
is `GEOMETRY`, not `GEOGRAPHY`, to match `zones.boundary`: zone
auto-assignment runs `ST_Contains(zones.boundary, issues.location)`, and
`ST_Contains` has no geography overload — mixing the two types would force a
cast on every insert and defeat both GIST indexes.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from geoalchemy2 import Geometry
from sqlalchemy import (
    Computed,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.database import Base

if TYPE_CHECKING:
    from app.models.authority_user import AuthorityUser
    from app.models.department import Department
    from app.models.issue_image import IssueImage
    from app.models.user import User
    from app.models.zone import Zone


class Issue(Base):
    __tablename__ = "issues"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    issue_number: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    reporter_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    category: Mapped[str] = mapped_column(
        Enum(
            "POTHOLE",
            "GARBAGE_ACCUMULATION",
            "WATER_LOGGING",
            "BROKEN_STREET_LIGHT",
            "DAMAGED_FOOTPATH",
            "SEWAGE_OVERFLOW",
            "OTHER",
            name="issue_category",
            create_type=False,
        ),
        nullable=False,
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        Enum(
            "REPORTED",
            "IN_PROGRESS",
            "RESOLVED",
            "REJECTED",
            name="issue_status",
            create_type=False,
        ),
        nullable=False,
        server_default="REPORTED",
    )

    # ── Geospatial ───────────────────────────────────────────────────────
    latitude: Mapped[Decimal] = mapped_column(Numeric(10, 7), nullable=False)
    longitude: Mapped[Decimal] = mapped_column(Numeric(10, 7), nullable=False)
    location: Mapped[str] = mapped_column(
        # spatial_index=False: the GIST index is declared explicitly below so
        # that it matches the name used by migration 009.
        Geometry(geometry_type="POINT", srid=4326, spatial_index=False),
        Computed("ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)", persisted=True),
        nullable=False,
    )
    address_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    zone_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("zones.id", ondelete="SET NULL"),
        nullable=True,
    )

    # ── Department routing ───────────────────────────────────────────────
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("departments.id", ondelete="SET NULL"),
        nullable=True,
    )

    # ── Community priority ───────────────────────────────────────────────
    upvote_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    # ── Assignment ───────────────────────────────────────────────────────
    assigned_to_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("authority_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # ── Resolution ───────────────────────────────────────────────────────
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Timestamps ───────────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    # ── Relationships ────────────────────────────────────────────────────
    # One-directional on purpose: User/Zone/Department/AuthorityUser are owned
    # by earlier tasks and are not edited here, so no `back_populates` pair is
    # declared against them.
    reporter: Mapped[User | None] = relationship("User", lazy="noload")
    zone: Mapped[Zone | None] = relationship("Zone", lazy="noload")
    department: Mapped[Department | None] = relationship("Department", lazy="noload")
    assigned_to: Mapped[AuthorityUser | None] = relationship("AuthorityUser", lazy="noload")
    images: Mapped[list[IssueImage]] = relationship(
        "IssueImage",
        back_populates="issue",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    __table_args__ = (
        # The map query is the hot path and the reason PostGIS is here.
        Index("idx_issues_location_gist", "location", postgresql_using="gist"),
        Index("idx_issues_status", "status"),
        Index("idx_issues_category", "category"),
        Index("idx_issues_zone", "zone_id"),
        Index("idx_issues_department", "department_id"),
        Index("idx_issues_reporter", "reporter_id", postgresql_where="reporter_id IS NOT NULL"),
        Index("idx_issues_created", created_at.desc()),
        # Authority dashboard list: zone filter + status filter + newest first.
        Index("idx_issues_zone_status_created", "zone_id", "status", created_at.desc()),
    )
