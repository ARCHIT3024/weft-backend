"""SQLAlchemy ORM model for the `issue_status_history` table.

Per `05_weft_backend_schema.md` Section 3.10.

An immutable audit log of every status transition. **Never updated, never
deleted** — that is the entire point of the table, and it is what lets the
citizen app render a truthful timeline and lets an authority answer "who moved
this to RESOLVED, and when?".

`previous_status` is NULL for exactly one row per issue: the initial `REPORTED`
entry written at submission. Any later row has both sides populated.

`changed_by_id` points at `users.id`, not `authority_users.id`, because the
actor is a person: an authority is a `users` row with `role = AUTHORITY` plus an
`authority_users` profile. It is nullable and `ON DELETE SET NULL` so that
removing a staff account never destroys the audit trail — the transition still
happened, and losing that is worse than losing the attribution.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.issue import Issue
    from app.models.user import User

# Both columns reuse the `issue_status` type created in migration 002.
# `create_type=False` is mandatory: without it SQLAlchemy emits its own
# CREATE TYPE and the migration fails on an already-existing enum.
_ISSUE_STATUS = Enum(
    "REPORTED",
    "IN_PROGRESS",
    "RESOLVED",
    "REJECTED",
    name="issue_status",
    create_type=False,
)


class IssueStatusHistory(Base):
    __tablename__ = "issue_status_history"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    issue_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("issues.id", ondelete="CASCADE"),
        nullable=False,
    )
    previous_status: Mapped[str | None] = mapped_column(_ISSUE_STATUS, nullable=True)
    new_status: Mapped[str] = mapped_column(_ISSUE_STATUS, nullable=False)
    changed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        # `clock_timestamp()`, not `NOW()`: NOW() is transaction_timestamp() and
        # is constant for a whole transaction, so two transitions written in one
        # transaction would claim the same instant and `ORDER BY created_at`
        # could return the timeline in either order. See migration 015.
        server_default=text("clock_timestamp()"),
    )

    issue: Mapped[Issue] = relationship("Issue", lazy="noload")
    changed_by: Mapped[User | None] = relationship("User", lazy="noload")

    __table_args__ = (
        # The timeline query: every entry for one issue, newest first.
        Index("idx_status_history_issue", "issue_id", created_at.desc()),
        Index(
            "idx_status_history_changed",
            "changed_by_id",
            postgresql_where="changed_by_id IS NOT NULL",
        ),
    )
