"""Create department_categories table

Revision ID: 007
Revises: 006
Create Date: 2026-08-18

Per 05_weft_backend_schema.md Section 3.3.
Composite PK (department_id, category) drives category → department routing.
Seed rows are inserted later by migration 018.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "007"
down_revision: str | None = "006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "department_categories",
        sa.Column("department_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "category",
            postgresql.ENUM(
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
        ),
        sa.ForeignKeyConstraint(
            ["department_id"],
            ["departments.id"],
            name="department_categories_department_id_fkey",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("department_id", "category", name="department_categories_pkey"),
    )


def downgrade() -> None:
    op.drop_table("department_categories")
