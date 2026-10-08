"""SQLAlchemy ORM model for the `departments` table.

Municipal departments (Roads, Sanitation, Water Supply, Electricity, ...).
A department owns the SLA threshold used for breach detection, the set of
issue categories it is responsible for (`department_categories`), and the
geographic zones assigned to it (`zones`).

`upvote_alert_threshold` is the per-department upvote count at which an
`issue.high_upvote_alert` event is published.  It is pulled forward into
Phase 1 (originally task 4.15a) because tasks 1.27/1.28 publish the event
and task 3.11 consumes it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.database import Base

if TYPE_CHECKING:
    from app.models.authority_user import AuthorityUser
    from app.models.department_category import DepartmentCategory
    from app.models.zone import Zone


class Department(Base):
    __tablename__ = "departments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    code: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    contact_email: Mapped[str | None] = mapped_column(String(255), nullable=True)

    sla_hours: Mapped[int] = mapped_column(Integer, nullable=False, server_default="72")
    upvote_alert_threshold: Mapped[int] = mapped_column(Integer, nullable=False, server_default="10")

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    # ── Relationships ────────────────────────────────────────────────────
    categories: Mapped[list[DepartmentCategory]] = relationship(
        "DepartmentCategory",
        back_populates="department",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    zones: Mapped[list[Zone]] = relationship("Zone", back_populates="department", lazy="noload")
    authority_users: Mapped[list[AuthorityUser]] = relationship(
        "AuthorityUser", back_populates="department", lazy="noload"
    )
