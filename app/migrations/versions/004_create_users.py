"""Create users table

Revision ID: 004
Revises: 003
Create Date: 2026-08-18

Per 05_weft_backend_schema.md Section 3.1.  Mirrors app/models/user.py.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "004"
down_revision: str | None = "003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("email", sa.String(length=255), nullable=True, unique=True),
        sa.Column("phone", sa.String(length=20), nullable=True, unique=True),
        sa.Column("name", sa.String(length=100), nullable=True),
        sa.Column("oauth_provider", sa.String(length=20), nullable=True),
        sa.Column("oauth_subject", sa.String(length=255), nullable=True),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column(
            "role",
            postgresql.ENUM("CITIZEN", "AUTHORITY", "ADMIN", name="user_role", create_type=False),
            nullable=False,
            server_default="CITIZEN",
        ),
        sa.Column(
            "trust_score",
            sa.Numeric(precision=5, scale=2),
            nullable=False,
            server_default="0.00",
        ),
        sa.Column("total_points", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("title", sa.String(length=50), nullable=True),
        sa.Column("is_anonymous", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("preferred_lang", sa.String(length=10), nullable=False, server_default="en"),
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
        sa.CheckConstraint(
            "(is_anonymous = true) OR (email IS NOT NULL) OR (phone IS NOT NULL)",
            name="users_email_or_anon",
        ),
        sa.CheckConstraint(
            "(oauth_provider IS NOT NULL) OR (password_hash IS NOT NULL) OR (is_anonymous = true)",
            name="users_oauth_or_password",
        ),
        sa.UniqueConstraint("oauth_provider", "oauth_subject", name="users_oauth_subject_unique"),
    )

    op.create_index(
        "idx_users_email",
        "users",
        ["email"],
        postgresql_where=sa.text("email IS NOT NULL"),
    )
    op.create_index(
        "idx_users_phone",
        "users",
        ["phone"],
        postgresql_where=sa.text("phone IS NOT NULL"),
    )
    op.create_index("idx_users_role", "users", ["role"])
    op.create_index("idx_users_trust_score", "users", [sa.text("trust_score DESC")])
    op.create_index("idx_users_total_points", "users", [sa.text("total_points DESC")])


def downgrade() -> None:
    op.drop_index("idx_users_total_points", table_name="users")
    op.drop_index("idx_users_trust_score", table_name="users")
    op.drop_index("idx_users_role", table_name="users")
    op.drop_index("idx_users_phone", table_name="users")
    op.drop_index("idx_users_email", table_name="users")
    op.drop_table("users")
