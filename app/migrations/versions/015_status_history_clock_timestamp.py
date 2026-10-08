"""Use clock_timestamp() for issue_status_history.created_at

Revision ID: 015
Revises: 014
Create Date: 2026-09-09

`NOW()` in PostgreSQL is `transaction_timestamp()` — it returns the moment the
*transaction* began and is constant for its whole duration. For most tables
that is fine and even desirable. For an append-only audit log it is wrong, and
in two ways:

1. **The recorded time is not when the event happened.** Two transitions
   written in one transaction both claim the transaction's start time, so the
   log misreports its own history.
2. **Ordering becomes non-deterministic.** `ORDER BY created_at DESC` cannot
   separate rows that share a timestamp, so the timeline an authority sees
   could come back in either order — and it silently varies.

`clock_timestamp()` reads the actual wall clock at statement execution, so rows
written microseconds apart are distinguishable and ordering is stable.

This surfaced as a failing integration test, where the test fixture wraps a
whole test in one transaction and two transitions therefore tied. Production
requests each get their own transaction, so the ordering bug was latent there
rather than absent — the fixture made a real fragility reproducible.

Only the DEFAULT changes. Existing rows keep the timestamps they have; there is
nothing to backfill, and rewriting historical audit timestamps would be exactly
the wrong instinct for an immutable log.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "015"
down_revision: str | None = "014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "issue_status_history",
        "created_at",
        server_default=sa.text("clock_timestamp()"),
        existing_type=sa.DateTime(timezone=True),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "issue_status_history",
        "created_at",
        server_default=sa.text("NOW()"),
        existing_type=sa.DateTime(timezone=True),
        existing_nullable=False,
    )
