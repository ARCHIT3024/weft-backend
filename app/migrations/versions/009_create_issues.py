"""Create issues table

Revision ID: 009
Revises: 008
Create Date: 2026-08-18

Per 05_weft_backend_schema.md Section 3.7, reduced to the MVP subset.

Omitted from the schema doc because the feature that needed them is cut from
the local MVP: `ai_suggested_category`, `ai_confidence` (AI categorisation),
`phash` (perceptual-hash dedupe), `is_flagged` (image moderation),
`captured_at` (offline capture queue).  The matching indexes
(`idx_issues_phash`, `idx_issues_sla_check`, `idx_issues_resolved`,
`idx_issues_upvotes`) go with them.

`upvote_count` is kept — the map UI renders it — but the doc's trigger that
syncs it from the `upvotes` table is NOT created: the MVP has no upvote
endpoint and no `upvotes` table.

`location` is a STORED generated column, GEOMETRY(POINT, 4326) to match
`zones.boundary`, so `ST_Contains` zone lookup stays index-only on both
sides.  Its GIST index is created explicitly (spatial_index=False on the
column) so its name is pinned to `idx_issues_location_gist`.
"""

from __future__ import annotations

from collections.abc import Sequence

import geoalchemy2
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "009"
down_revision: str | None = "008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "issues",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("issue_number", sa.String(length=20), nullable=False, unique=True),
        sa.Column("reporter_id", postgresql.UUID(as_uuid=True), nullable=True),
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
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(
                "REPORTED",
                "IN_PROGRESS",
                "RESOLVED",
                "REJECTED",
                name="issue_status",
                create_type=False,
            ),
            nullable=False,
            server_default="REPORTED",
        ),
        # ── Geospatial ───────────────────────────────────────────────────
        sa.Column("latitude", sa.Numeric(precision=10, scale=7), nullable=False),
        sa.Column("longitude", sa.Numeric(precision=10, scale=7), nullable=False),
        sa.Column(
            "location",
            geoalchemy2.Geometry(geometry_type="POINT", srid=4326, spatial_index=False),
            sa.Computed("ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)", persisted=True),
            nullable=False,
        ),
        sa.Column("address_text", sa.Text(), nullable=True),
        sa.Column("zone_id", postgresql.UUID(as_uuid=True), nullable=True),
        # ── Department routing ───────────────────────────────────────────
        sa.Column("department_id", postgresql.UUID(as_uuid=True), nullable=True),
        # ── Community priority ───────────────────────────────────────────
        sa.Column("upvote_count", sa.Integer(), nullable=False, server_default="0"),
        # ── Assignment ───────────────────────────────────────────────────
        sa.Column("assigned_to_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=True),
        # ── Resolution ───────────────────────────────────────────────────
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        # ── Timestamps ───────────────────────────────────────────────────
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.ForeignKeyConstraint(
            ["reporter_id"],
            ["users.id"],
            name="issues_reporter_id_fkey",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["zone_id"],
            ["zones.id"],
            name="issues_zone_id_fkey",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["department_id"],
            ["departments.id"],
            name="issues_department_id_fkey",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["assigned_to_id"],
            ["authority_users.id"],
            name="issues_assigned_to_id_fkey",
            ondelete="SET NULL",
        ),
    )

    # Geospatial index — the nearby-issues map query depends on it.
    op.create_index("idx_issues_location_gist", "issues", ["location"], postgresql_using="gist")

    # Filtering & sorting indexes.
    op.create_index("idx_issues_status", "issues", ["status"])
    op.create_index("idx_issues_category", "issues", ["category"])
    op.create_index("idx_issues_zone", "issues", ["zone_id"])
    op.create_index("idx_issues_department", "issues", ["department_id"])
    op.create_index(
        "idx_issues_reporter",
        "issues",
        ["reporter_id"],
        postgresql_where=sa.text("reporter_id IS NOT NULL"),
    )
    op.create_index("idx_issues_created", "issues", [sa.text("created_at DESC")])

    # Composite: authority dashboard list (zone + status, newest first).
    op.create_index(
        "idx_issues_zone_status_created",
        "issues",
        ["zone_id", "status", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("idx_issues_zone_status_created", table_name="issues")
    op.drop_index("idx_issues_created", table_name="issues")
    op.drop_index("idx_issues_reporter", table_name="issues")
    op.drop_index("idx_issues_department", table_name="issues")
    op.drop_index("idx_issues_zone", table_name="issues")
    op.drop_index("idx_issues_category", table_name="issues")
    op.drop_index("idx_issues_status", table_name="issues")
    op.drop_index("idx_issues_location_gist", table_name="issues", postgresql_using="gist")
    op.drop_table("issues")
