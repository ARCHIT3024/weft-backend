"""Create all enum types

Revision ID: 002
Revises: 001
Create Date: 2026-07-22

All enum types defined in 05_weft_backend_schema.md Section 2.
Must run before any table migration that references these types.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "002"
down_revision: str | None = "001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── user_role ────────────────────────────────────────────────────
    op.execute(
        """
        CREATE TYPE user_role AS ENUM (
            'CITIZEN',
            'AUTHORITY',
            'ADMIN'
        );
    """
    )

    # ── issue_status ────────────────────────────────────────────────
    op.execute(
        """
        CREATE TYPE issue_status AS ENUM (
            'REPORTED',
            'IN_PROGRESS',
            'RESOLVED',
            'REJECTED'
        );
    """
    )

    # ── issue_category ──────────────────────────────────────────────
    op.execute(
        """
        CREATE TYPE issue_category AS ENUM (
            'POTHOLE',
            'GARBAGE_ACCUMULATION',
            'WATER_LOGGING',
            'BROKEN_STREET_LIGHT',
            'DAMAGED_FOOTPATH',
            'SEWAGE_OVERFLOW',
            'OTHER'
        );
    """
    )

    # ── notification_type ───────────────────────────────────────────
    op.execute(
        """
        CREATE TYPE notification_type AS ENUM (
            'STATUS_CHANGE',
            'ISSUE_ASSIGNED',
            'UPVOTE_MILESTONE',
            'GAMIFICATION_REWARD',
            'SYSTEM'
        );
    """
    )

    # ── notification_channel ────────────────────────────────────────
    op.execute(
        """
        CREATE TYPE notification_channel AS ENUM (
            'PUSH',
            'IN_APP',
            'EMAIL'
        );
    """
    )

    # ── image_type ──────────────────────────────────────────────────
    op.execute(
        """
        CREATE TYPE image_type AS ENUM (
            'REPORT',
            'RESOLUTION_PROOF'
        );
    """
    )

    # ── gamification_event_type ─────────────────────────────────────
    op.execute(
        """
        CREATE TYPE gamification_event_type AS ENUM (
            'ISSUE_SUBMITTED',
            'ISSUE_RESOLVED',
            'UPVOTE_GIVEN',
            'UPVOTE_RECEIVED',
            'SPAM_PENALTY'
        );
    """
    )


def downgrade() -> None:
    op.execute("DROP TYPE IF EXISTS gamification_event_type CASCADE;")
    op.execute("DROP TYPE IF EXISTS image_type CASCADE;")
    op.execute("DROP TYPE IF EXISTS notification_channel CASCADE;")
    op.execute("DROP TYPE IF EXISTS notification_type CASCADE;")
    op.execute("DROP TYPE IF EXISTS issue_category CASCADE;")
    op.execute("DROP TYPE IF EXISTS issue_status CASCADE;")
    op.execute("DROP TYPE IF EXISTS user_role CASCADE;")
