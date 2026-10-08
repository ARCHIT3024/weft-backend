"""SQLAlchemy ORM model for the `department_categories` table.

Association table mapping which issue categories belong to which department.
This drives auto-routing: on issue insert the reported category is looked up
here to resolve `issues.department_id`.

Composite primary key `(department_id, category)` guarantees a category is
mapped at most once per department.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Enum, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.department import Department


class DepartmentCategory(Base):
    __tablename__ = "department_categories"

    department_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("departments.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
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
        primary_key=True,
        nullable=False,
    )

    # ── Relationships ────────────────────────────────────────────────────
    department: Mapped[Department] = relationship("Department", back_populates="categories", lazy="selectin")
