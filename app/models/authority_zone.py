"""SQLAlchemy ORM model for the `authority_zones` table.

Many-to-many association table: which zones an authority user is responsible
for.  Used to scope every authority dashboard query (`issues.zone_id = ANY(...)`).

Composite primary key `(authority_user_id, zone_id)` prevents duplicate
assignments; `idx_auth_zones_zone` supports the reverse lookup (which
authorities cover a given zone).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.authority_user import AuthorityUser
    from app.models.zone import Zone


class AuthorityZone(Base):
    __tablename__ = "authority_zones"

    authority_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("authority_users.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    zone_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("zones.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )

    # ── Relationships ────────────────────────────────────────────────────
    authority_user: Mapped[AuthorityUser] = relationship("AuthorityUser", back_populates="zone_links", lazy="noload")
    zone: Mapped[Zone] = relationship("Zone", back_populates="authority_links", lazy="selectin")

    __table_args__ = (Index("idx_auth_zones_zone", "zone_id"),)
