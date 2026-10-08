"""Create user_fcm_tokens table

Revision ID: 016
Revises: 015
Create Date: 2026-10-09

Per 05_weft_backend_schema.md Section 3.11. Tasks 1.10 / 1.25 / 2.31.

Three deliberate departures from the schema doc:

1. **`UNIQUE (device_token)` replaces `UNIQUE (user_id, device_id)`.** A
   registration token addresses one app install, and a phone has one current
   user at a time. When someone else logs in on the same phone and registers
   the same token, the row must *move* to them — `INSERT ... ON CONFLICT
   (device_token) DO UPDATE SET user_id = ...` — or the previous user's
   notifications (issue numbers, their addresses) keep arriving on a phone
   they handed over. The doc's constraint permitted the same token under two
   users at once, which is exactly that leak.
2. **No `is_active`.** A token FCM reports `UNREGISTERED` is deleted outright.
   It can never work again, and a soft-delete flag that every query must
   remember to filter on buys nothing.
3. **No `device_platform` / `device_id`.** FCM HTTP v1 addresses Android and
   iOS identically by token; nothing would read them, and the contract's
   `PATCH /users/me` body cannot supply them. Adding a nullable column later
   is cheap if a use appears.

The partial index `idx_fcm_tokens_user_active` becomes a plain index on
`user_id` for the same reason `is_active` is gone.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "016"
down_revision: str | None = "015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_fcm_tokens",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # FCM registration token. Rotates; the app re-registers on each launch.
        sa.Column("device_token", sa.Text(), nullable=False),
        sa.Column(
            "last_seen",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        # One owner per token — the upsert target. See departure 1 above.
        sa.UniqueConstraint("device_token", name="user_fcm_tokens_device_token_key"),
    )

    # Every device of one user: the lookup before each push.
    op.create_index("idx_fcm_tokens_user", "user_fcm_tokens", ["user_id"])


def downgrade() -> None:
    op.drop_index("idx_fcm_tokens_user", table_name="user_fcm_tokens")
    op.drop_table("user_fcm_tokens")
