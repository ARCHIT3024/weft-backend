"""Create zones table

Revision ID: 006
Revises: 005
Create Date: 2026-08-18

Per 05_weft_backend_schema.md Section 3.4.
`boundary` is GEOMETRY(POLYGON, 4326); the GIST index is required for the
ST_Contains zone auto-assignment performed on every issue insert.

The spatial index is created explicitly (spatial_index=False on the column)
so its name is pinned to `idx_zones_boundary` rather than left to GeoAlchemy2.
"""

from __future__ import annotations

from collections.abc import Sequence

import geoalchemy2
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "006"
down_revision: str | None = "005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "zones",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("department_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "boundary",
            geoalchemy2.Geometry(geometry_type="POLYGON", srid=4326, spatial_index=False),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.ForeignKeyConstraint(
            ["department_id"],
            ["departments.id"],
            name="zones_department_id_fkey",
            ondelete="SET NULL",
        ),
    )

    op.create_index("idx_zones_boundary", "zones", ["boundary"], postgresql_using="gist")


def downgrade() -> None:
    op.drop_index("idx_zones_boundary", table_name="zones", postgresql_using="gist")
    op.drop_table("zones")
