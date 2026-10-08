"""Create issue_status_history table

Revision ID: 013
Revises: 012
Create Date: 2026-09-08

Per 05_weft_backend_schema.md Section 3.10. Task 1.21's audit trail.

An append-only log of status transitions. No UPDATE and no DELETE path exists
by design; `ON DELETE CASCADE` from `issues` is the only way a row leaves.

`previous_status` is nullable because the first entry for an issue — the
`REPORTED` row written at submission — has no predecessor.

Both status columns reuse the `issue_status` enum created in migration 002, so
`postgresql.ENUM(..., create_type=False)` is required here. Without it Alembic
emits a second CREATE TYPE and the migration aborts on the existing type.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "013"
down_revision: str | None = "012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ISSUE_STATUS = postgresql.ENUM(
    "REPORTED",
    "IN_PROGRESS",
    "RESOLVED",
    "REJECTED",
    name="issue_status",
    create_type=False,
)


def upgrade() -> None:
    op.create_table(
        "issue_status_history",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "issue_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("issues.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # NULL only for the initial REPORTED entry.
        sa.Column("previous_status", ISSUE_STATUS, nullable=True),
        sa.Column("new_status", ISSUE_STATUS, nullable=False),
        # users.id, not authority_users.id — the actor is a person. SET NULL so
        # deleting a staff account never destroys the audit trail.
        sa.Column(
            "changed_by_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )

    # The timeline query: all entries for one issue, newest first.
    op.create_index(
        "idx_status_history_issue",
        "issue_status_history",
        ["issue_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "idx_status_history_changed",
        "issue_status_history",
        ["changed_by_id"],
        postgresql_where=sa.text("changed_by_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_status_history_changed", table_name="issue_status_history")
    op.drop_index("idx_status_history_issue", table_name="issue_status_history")
    op.drop_table("issue_status_history")
