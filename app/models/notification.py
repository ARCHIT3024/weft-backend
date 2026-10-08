"""SQLAlchemy ORM model for the `notifications` table.

Per `05_weft_backend_schema.md` Section 3.12.

**This table is the source of truth for what a user was told**, not FCM. A row
is written for every notification whether or not a push is ever delivered —
the user may have no registered device, FCM may be down, or push may simply
not be configured — and the in-app notification centre (`GET /notifications`)
reads only this table. A push is a best-effort echo of a row, never the other
way round.

`channel` records how delivery was attempted: `PUSH` when the user had at
least one registered device at send time, `IN_APP` when they had none and the
notification centre is the only place it will ever appear.

`sent_at` is NULL until FCM accepts the message for at least one of the user's
devices. `retry_count` is the number of failed FCM attempts across all of them.
A `PUSH` row with `sent_at IS NULL AND retry_count < 3` is what a future
re-delivery job would pick up (`idx_notifications_pending`).

`issue_id` is `ON DELETE SET NULL`: deleting an issue must not erase the
record that its reporter was notified about it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, SmallInteger, String, Text, text
from sqlalchemy.dialects.postgresql import ENUM, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.issue import Issue
    from app.models.user import User

# Both enum types already exist — migration 002 created them in Phase 0.
# `create_type=False` is mandatory, or SQLAlchemy emits a second CREATE TYPE.
# It is the PostgreSQL `ENUM`, not the generic `sqlalchemy.Enum`, on purpose:
# the generic type accepts `create_type=False` and silently ignores it — its
# PostgreSQL implementation still reports `create_type=True`.
_NOTIFICATION_TYPE = ENUM(
    "STATUS_CHANGE",
    "ISSUE_ASSIGNED",
    "UPVOTE_MILESTONE",
    "GAMIFICATION_REWARD",
    "SYSTEM",
    name="notification_type",
    create_type=False,
)
_NOTIFICATION_CHANNEL = ENUM("PUSH", "IN_APP", "EMAIL", name="notification_channel", create_type=False)


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    issue_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("issues.id", ondelete="SET NULL"),
        nullable=True,
    )
    type: Mapped[str] = mapped_column(_NOTIFICATION_TYPE, nullable=False)
    channel: Mapped[str] = mapped_column(_NOTIFICATION_CHANNEL, nullable=False, server_default="PUSH")
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retry_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        # `clock_timestamp()`, not `NOW()`, for the reason D-13 gives for the
        # audit log: the list is ordered newest-first, and two notifications
        # written in one transaction must not tie.
        server_default=text("clock_timestamp()"),
    )

    user: Mapped[User] = relationship("User", lazy="noload")
    issue: Mapped[Issue | None] = relationship("Issue", lazy="noload")

    __table_args__ = (
        # The notification centre: one user's rows, newest first.
        Index("idx_notifications_user_created", "user_id", created_at.desc()),
        # The unread badge and the unread-only filter.
        Index(
            "idx_notifications_user_unread",
            "user_id",
            created_at.desc(),
            postgresql_where=text("is_read = false"),
        ),
        # Undelivered pushes still worth retrying.
        Index(
            "idx_notifications_pending",
            "created_at",
            postgresql_where=text("sent_at IS NULL AND retry_count < 3 AND channel = 'PUSH'"),
        ),
    )
