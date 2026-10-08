"""Enable PostGIS extension

Revision ID: 001
Revises: None
Create Date: 2026-07-22

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis;")


def downgrade() -> None:
    op.execute("DROP EXTENSION IF EXISTS postgis CASCADE;")
