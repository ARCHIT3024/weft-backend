"""Create authority_zones table

Revision ID: 008
Revises: 007
Create Date: 2026-08-18

Per 05_weft_backend_schema.md Section 3.6.
Many-to-many between authority_users and zones; both FK targets are created
by earlier revisions (005 and 006 respectively).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "008"
down_revision: str | None = "007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "authority_zones",
        sa.Column("authority_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("zone_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["authority_user_id"],
            ["authority_users.id"],
            name="authority_zones_authority_user_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["zone_id"],
            ["zones.id"],
            name="authority_zones_zone_id_fkey",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("authority_user_id", "zone_id", name="authority_zones_pkey"),
    )

    op.create_index("idx_auth_zones_zone", "authority_zones", ["zone_id"])


def downgrade() -> None:
    op.drop_index("idx_auth_zones_zone", table_name="authority_zones")
    op.drop_table("authority_zones")
