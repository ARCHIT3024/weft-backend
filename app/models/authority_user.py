"""SQLAlchemy ORM model for the `authority_users` table.

Extends `users` with authority-specific metadata (department membership,
employee ID, designation, department-admin flag).  An authority user must
have `users.role = 'AUTHORITY'` or `'ADMIN'`; that invariant is enforced at
the service layer because it spans two tables.

`created_by` is a self-referential FK recording which authority account
provisioned this one.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.database import Base

if TYPE_CHECKING:
    from app.models.authority_zone import AuthorityZone
    from app.models.department import Department
    from app.models.user import User


class AuthorityUser(Base):
    __tablename__ = "authority_users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    department_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("departments.id"), nullable=False)
    employee_id: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    designation: Mapped[str | None] = mapped_column(String(100), nullable=True)

    is_dept_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("authority_users.id", ondelete="SET NULL"),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    # ── Relationships ────────────────────────────────────────────────────
    user: Mapped[User] = relationship("User", back_populates="authority_profile", lazy="selectin")
    department: Mapped[Department] = relationship("Department", back_populates="authority_users", lazy="selectin")
    zone_links: Mapped[list[AuthorityZone]] = relationship(
        "AuthorityZone",
        back_populates="authority_user",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    creator: Mapped[AuthorityUser | None] = relationship(
        "AuthorityUser",
        remote_side="AuthorityUser.id",
        back_populates="provisioned_users",
        lazy="noload",
    )
    provisioned_users: Mapped[list[AuthorityUser]] = relationship(
        "AuthorityUser", back_populates="creator", lazy="noload"
    )

    __table_args__ = (
        Index("idx_authority_users_dept", "department_id"),
        Index("idx_authority_users_userid", "user_id"),
    )
