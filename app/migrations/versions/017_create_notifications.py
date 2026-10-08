"""Create notifications table

Revision ID: 017
Revises: 016
Create Date: 2026-10-09

Per 05_weft_backend_schema.md Section 3.12. Tasks 1.25 / 1.26.

The table is the in-app notification centre's source of truth: a row is
written for every notification whether or not a push is ever delivered.

Both enum types — `notification_type` and `notification_channel` — were
created by migration 002 in Phase 0, so they are referenced here with
`create_type=False`. Without it Alembic emits a second CREATE TYPE and the
migration aborts on the existing type; and downgrade must not drop them,
because 002 owns them.

Departures from the schema doc, both small:

1. **`created_at` defaults to `clock_timestamp()`, not `NOW()`** — D-13's
   reasoning applies unchanged. The notification centre is ordered newest
   first, and `NOW()` is constant for a whole transaction, so two
   notifications written together would tie and could come back in either
   order.
2. **Indexes.** The doc's partial `idx_notifications_user_unread` serves only
   unread rows, but the notification centre lists read and unread together,
   so `idx_notifications_user_created` covers that query. And
   `idx_notifications_pending` gains `AND channel = 'PUSH'`: an `IN_APP` row
   (the user had no device registered) is never "pending" — there is nowhere
   to push it — and without the predicate a re-delivery job would chase those
   rows forever.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "017"
down_revision: str | None = "016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOTIFICATION_TYPE = postgresql.ENUM(
    "STATUS_CHANGE",
    "ISSUE_ASSIGNED",
    "UPVOTE_MILESTONE",
    "GAMIFICATION_REWARD",
    "SYSTEM",
    name="notification_type",
    create_type=False,
)

NOTIFICATION_CHANNEL = postgresql.ENUM(
    "PUSH",
    "IN_APP",
    "EMAIL",
    name="notification_channel",
    create_type=False,
)


def upgrade() -> None:
    op.create_table(
        "notifications",
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
        # SET NULL: deleting an issue must not erase the record that its
        # reporter was told about it.
        sa.Column(
            "issue_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("issues.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("type", NOTIFICATION_TYPE, nullable=False),
        sa.Column("channel", NOTIFICATION_CHANNEL, nullable=False, server_default="PUSH"),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("is_read", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
        # NULL until FCM accepts the message for at least one device.
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        # Failed FCM attempts, summed across the user's devices.
        sa.Column("retry_count", sa.SmallInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
    )

    # The notification centre: one user's rows, newest first.
    op.create_index(
        "idx_notifications_user_created",
        "notifications",
        ["user_id", sa.text("created_at DESC")],
    )
    # The unread badge and the unread-only filter.
    op.create_index(
        "idx_notifications_user_unread",
        "notifications",
        ["user_id", sa.text("created_at DESC")],
        postgresql_where=sa.text("is_read = FALSE"),
    )
    # Undelivered pushes still worth retrying.
    op.create_index(
        "idx_notifications_pending",
        "notifications",
        ["created_at"],
        postgresql_where=sa.text("sent_at IS NULL AND retry_count < 3 AND channel = 'PUSH'"),
    )


def downgrade() -> None:
    # The enum types are left alone: migration 002 created them and its own
    # downgrade drops them.
    op.drop_index("idx_notifications_pending", table_name="notifications")
    op.drop_index("idx_notifications_user_unread", table_name="notifications")
    op.drop_index("idx_notifications_user_created", table_name="notifications")
    op.drop_table("notifications")
