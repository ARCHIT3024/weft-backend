"""Create refresh_tokens table

Revision ID: 011
Revises: 010
Create Date: 2026-08-18

Per 05_weft_backend_schema.md Section 3.14.

This table's model (`app/models/refresh_token.py`) landed early, under task 1.1,
because `app/models/user.py` declares a `refresh_tokens` relationship against it.
The original plan put its migration in task 1.14's 009-017 batch, but 009 and 010
were taken by the issues data layer, so it lands here as 011.

Only the SHA-256 hash of a token is stored, never the raw value, so a database
leak cannot be replayed as a valid refresh token. Rotation revokes the previous
row rather than deleting it, which keeps token-reuse detection possible.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "011"
down_revision: str | None = "010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "refresh_tokens",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_revoked", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("device_id", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("token_hash"),
    )

    # Lookup on rotation is always "this user's tokens that are still live", so
    # the partial index keeps revoked history out of the hot path.
    op.create_index(
        "idx_refresh_tokens_user_active",
        "refresh_tokens",
        ["user_id"],
        postgresql_where=sa.text("is_revoked = false"),
    )
    # Verifying a presented token is a lookup by hash.
    op.create_index("idx_refresh_tokens_hash", "refresh_tokens", ["token_hash"])
    # Expiry sweeps scan by expires_at.
    op.create_index("idx_refresh_tokens_expires", "refresh_tokens", ["expires_at"])


def downgrade() -> None:
    op.drop_index("idx_refresh_tokens_expires", table_name="refresh_tokens")
    op.drop_index("idx_refresh_tokens_hash", table_name="refresh_tokens")
    op.drop_index("idx_refresh_tokens_user_active", table_name="refresh_tokens")
    op.drop_table("refresh_tokens")
