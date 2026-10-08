"""Create departments table

Revision ID: 003
Revises: 002
Create Date: 2026-08-18

Per 05_weft_backend_schema.md Section 3.2.

`upvote_alert_threshold` is included here (originally planned for task 4.15a)
because tasks 1.27/1.28 publish `issue.high_upvote_alert` and task 3.11
consumes it — leaving the column for Phase 4 would create a three-week
dangling dependency.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "003"
down_revision: str | None = "002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "departments",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("code", sa.String(length=20), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("contact_email", sa.String(length=255), nullable=True),
        sa.Column("sla_hours", sa.Integer(), nullable=False, server_default="72"),
        sa.Column("upvote_alert_threshold", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("departments")
