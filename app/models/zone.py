"""SQLAlchemy ORM model for the `zones` table.

Geographic jurisdictions.  Each zone optionally belongs to a department and
is defined as a WGS84 (SRID 4326) PostGIS polygon boundary.  The GIST index
on `boundary` is critical: every issue insert runs an `ST_Contains` lookup
against this table to auto-assign a zone.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from geoalchemy2 import Geometry
from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.database import Base

if TYPE_CHECKING:
    from app.models.authority_zone import AuthorityZone
    from app.models.department import Department


class Zone(Base):
    __tablename__ = "zones"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("departments.id", ondelete="SET NULL"),
        nullable=True,
    )
    boundary: Mapped[str] = mapped_column(
        # spatial_index=False: the GIST index is declared explicitly below so
        # that it matches the name used by migration 006.
        Geometry(geometry_type="POLYGON", srid=4326, spatial_index=False),
        nullable=False,
    )

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    # ── Relationships ────────────────────────────────────────────────────
    department: Mapped[Department | None] = relationship("Department", back_populates="zones", lazy="selectin")
    authority_links: Mapped[list[AuthorityZone]] = relationship(
        "AuthorityZone",
        back_populates="zone",
        cascade="all, delete-orphan",
        lazy="noload",
    )

    __table_args__ = (Index("idx_zones_boundary", "boundary", postgresql_using="gist"),)
