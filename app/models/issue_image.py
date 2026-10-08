"""SQLAlchemy ORM model for the `issue_images` table.

Metadata for photos attached to an issue: the citizen's report photo and,
later, the authority's resolution proof.  `image_type` separates the two.

MVP SCOPE — a deliberate subset of `05_weft_backend_schema.md` Section 3.8.
The binary lives on the local disk of the developer machine, not in S3, so
the doc's `s3_key` + `cdn_url` pair collapses into a single `file_path`
holding a path relative to the configured upload directory.  Omitted:

* `uploaded_by`                          — `image_type` already distinguishes
  citizen report from authority proof, and there is no per-uploader query.
* `file_size_bytes`, `width_px`, `height_px` — only image-processing and
  moderation used them; both are cut.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.database import Base

if TYPE_CHECKING:
    from app.models.issue import Issue


class IssueImage(Base):
    __tablename__ = "issue_images"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    issue_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("issues.id", ondelete="CASCADE"),
        nullable=False,
    )
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    image_type: Mapped[str] = mapped_column(
        Enum("REPORT", "RESOLUTION_PROOF", name="image_type", create_type=False),
        nullable=False,
        server_default="REPORT",
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    # ── Relationships ────────────────────────────────────────────────────
    issue: Mapped[Issue] = relationship("Issue", back_populates="images", lazy="noload")

    __table_args__ = (Index("idx_issue_images_issue", "issue_id"),)
