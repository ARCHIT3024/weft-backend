"""Create issue_images table

Revision ID: 010
Revises: 009
Create Date: 2026-08-18

Per 05_weft_backend_schema.md Section 3.8, reduced to the MVP subset.

The MVP stores uploads on the local disk of the developer machine, so the
doc's `s3_key` + `cdn_url` pair is replaced by a single `file_path` holding a
path relative to the configured upload directory.  Omitted: `uploaded_by`
(image_type already separates citizen report from authority proof) and
`file_size_bytes` / `width_px` / `height_px` (image processing and moderation
are cut).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "010"
down_revision: str | None = "009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "issue_images",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("issue_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("file_path", sa.Text(), nullable=False),
        sa.Column(
            "image_type",
            postgresql.ENUM(
                "REPORT",
                "RESOLUTION_PROOF",
                name="image_type",
                create_type=False,
            ),
            nullable=False,
            server_default="REPORT",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.ForeignKeyConstraint(
            ["issue_id"],
            ["issues.id"],
            name="issue_images_issue_id_fkey",
            ondelete="CASCADE",
        ),
    )

    op.create_index("idx_issue_images_issue", "issue_images", ["issue_id"])


def downgrade() -> None:
    op.drop_index("idx_issue_images_issue", table_name="issue_images")
    op.drop_table("issue_images")
