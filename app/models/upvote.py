"""SQLAlchemy ORM model for the `upvotes` table.

Per `05_weft_backend_schema.md` Section 3.9.

One row per (user, issue) pair. The composite primary key `(user_id, issue_id)`
is the whole duplicate-vote defence: a second upvote from the same user is a
primary-key violation at the database level, not a race the application has to
win with a read-then-write.

`issues.upvote_count` is a denormalised cache of `COUNT(*)` over this table. It
is maintained by the `trg_upvote_count` trigger created in migration 012, **not**
by the application — see that migration for why. Nothing in Python should ever
assign to `Issue.upvote_count`; read it, and let the trigger own it.

`anonymous` users have no row in `users` and therefore cannot upvote. That is
deliberate: an upvote is a priority signal, and an unauthenticated one is a
counter anybody can inflate.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.database import Base

if TYPE_CHECKING:
    from app.models.issue import Issue
    from app.models.user import User


class Upvote(Base):
    __tablename__ = "upvotes"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    issue_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("issues.id", ondelete="CASCADE"),
        primary_key=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    # One-directional, matching the convention in `issue.py`: the models owned by
    # earlier tasks are not edited to add the reverse side.
    user: Mapped[User] = relationship("User", lazy="noload")
    issue: Mapped[Issue] = relationship("Issue", lazy="noload")

    __table_args__ = (
        # The PK already covers (user_id, issue_id) in that order, which serves
        # "has this user upvoted?" but not "who upvoted this issue?" — the
        # leading column is wrong for that. This index covers the latter, and
        # the recount path if the denormalised counter ever has to be rebuilt.
        Index("idx_upvotes_issue", "issue_id"),
    )
