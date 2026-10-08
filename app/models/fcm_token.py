"""SQLAlchemy ORM model for the `user_fcm_tokens` table.

Per `05_weft_backend_schema.md` Section 3.11, with three deliberate departures
(migration 016 explains each):

* **`device_token` is UNIQUE on its own.** A registration token addresses one
  app install, and a phone has one current user. When a second person logs in
  on the same phone and registers, the token *moves* to them — an upsert on
  this constraint — instead of the first person's notifications continuing to
  arrive on a device they no longer use. The schema doc's
  `UNIQUE (user_id, device_id)` could not express that.
* **No `is_active`.** A token FCM reports dead is deleted, not flagged; a
  dead token has no further use, and a flag every query must remember to
  filter on is a bug waiting to happen.
* **No `device_platform` / `device_id`.** FCM HTTP v1 addresses Android and
  iOS identically by token, so nothing reads them, and the frozen contract's
  `PATCH /users/me` body has no field to supply them. Omitted rather than
  left as permanent NULLs — the same rule `issue.py` follows.

A token is a delivery address for one person's phone. It is never returned by
any endpoint and never logged in full (see `app.core.push.redact_token`).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.database import Base

if TYPE_CHECKING:
    from app.models.user import User


class FcmToken(Base):
    __tablename__ = "user_fcm_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    device_token: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    # Refreshed on every registration. The mobile app re-registers on each
    # launch (task 2.31), so this doubles as "last time this phone opened Weft"
    # and is what the per-user token cap evicts by.
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    # One-directional, matching `upvote.py`: earlier models are not edited to
    # add the reverse side.
    user: Mapped[User] = relationship("User", lazy="noload")

    __table_args__ = (
        # "Every device of this user" — the lookup before each push.
        Index("idx_fcm_tokens_user", "user_id"),
    )
