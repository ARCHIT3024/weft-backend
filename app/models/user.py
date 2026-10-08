"""SQLAlchemy ORM model for the `users` table.

All citizen and authority accounts share this table. The `role` field
differentiates them.  `authority_users` extends this table for
authority-specific attributes.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    phone: Mapped[str | None] = mapped_column(String(20), unique=True, nullable=True)
    name: Mapped[str | None] = mapped_column(String(100), nullable=True)

    oauth_provider: Mapped[str | None] = mapped_column(String(20), nullable=True)
    oauth_subject: Mapped[str | None] = mapped_column(String(255), nullable=True)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)

    role: Mapped[str] = mapped_column(
        Enum("CITIZEN", "AUTHORITY", "ADMIN", name="user_role", create_type=False),
        nullable=False,
        server_default="CITIZEN",
    )
    trust_score: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False, server_default="0.00")
    total_points: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    title: Mapped[str | None] = mapped_column(String(50), nullable=True)

    is_anonymous: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    preferred_lang: Mapped[str] = mapped_column(String(10), nullable=False, server_default="en")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    # ── Relationships ────────────────────────────────────────────────────
    authority_profile: Mapped[AuthorityUser | None] = relationship(
        "AuthorityUser", back_populates="user", uselist=False, lazy="selectin"
    )
    refresh_tokens: Mapped[list[RefreshToken]] = relationship("RefreshToken", back_populates="user", lazy="noload")

    __table_args__ = (
        CheckConstraint(
            "(is_anonymous = true) OR (email IS NOT NULL) OR (phone IS NOT NULL)",
            name="users_email_or_anon",
        ),
        CheckConstraint(
            "(oauth_provider IS NOT NULL) OR (password_hash IS NOT NULL) OR (is_anonymous = true)",
            name="users_oauth_or_password",
        ),
        UniqueConstraint("oauth_provider", "oauth_subject", name="users_oauth_subject_unique"),
        Index("idx_users_email", "email", postgresql_where="email IS NOT NULL"),
        Index("idx_users_phone", "phone", postgresql_where="phone IS NOT NULL"),
        Index("idx_users_role", "role"),
        Index("idx_users_trust_score", trust_score.desc()),
        Index("idx_users_total_points", total_points.desc()),
    )


# Avoid circular imports — these are imported at module level in __init__.py
from app.models.authority_user import AuthorityUser  # noqa: E402
from app.models.refresh_token import RefreshToken  # noqa: E402
