"""SQLAlchemy ORM model for the `refresh_tokens` table.

Persists refresh-token records for JWT rotation.  The raw token is never
stored — only its SHA-256 hash — so a database leak cannot be replayed as a
valid refresh token.  Rotation revokes the previous row rather than deleting
it, which keeps reuse detection possible.

NOTE: the migration that creates this table is task 1.14 (Week 3); this
model intentionally exists ahead of its table.  It is defined now because
`app/models/user.py` declares a `refresh_tokens` relationship against it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.database import Base

if TYPE_CHECKING:
    from app.models.user import User


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    device_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    # ── Relationships ────────────────────────────────────────────────────
    user: Mapped[User] = relationship("User", back_populates="refresh_tokens", lazy="noload")

    __table_args__ = (
        Index(
            "idx_refresh_tokens_user_active",
            "user_id",
            postgresql_where="is_revoked = false",
        ),
    )
