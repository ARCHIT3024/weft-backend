"""Create authority_users table

Revision ID: 005
Revises: 004
Create Date: 2026-08-18

Per 05_weft_backend_schema.md Section 3.5.
FKs → users (CASCADE), departments (NO ACTION), authority_users (self, SET NULL).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "005"
down_revision: str | None = "004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "authority_users",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("department_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("employee_id", sa.String(length=50), nullable=False, unique=True),
        sa.Column("designation", sa.String(length=100), nullable=True),
        sa.Column("is_dept_admin", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
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
            ["user_id"],
            ["users.id"],
            name="authority_users_user_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["department_id"],
            ["departments.id"],
            name="authority_users_department_id_fkey",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["authority_users.id"],
            name="authority_users_created_by_fkey",
            ondelete="SET NULL",
        ),
    )

    op.create_index("idx_authority_users_dept", "authority_users", ["department_id"])
    op.create_index("idx_authority_users_userid", "authority_users", ["user_id"])


def downgrade() -> None:
    op.drop_index("idx_authority_users_userid", table_name="authority_users")
    op.drop_index("idx_authority_users_dept", table_name="authority_users")
    op.drop_table("authority_users")
