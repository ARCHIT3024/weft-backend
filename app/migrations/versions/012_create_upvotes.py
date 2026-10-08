"""Create upvotes table + upvote_count sync trigger

Revision ID: 012
Revises: 011
Create Date: 2026-09-08

Per 05_weft_backend_schema.md Section 3.9. Task 1.24.

`issues.upvote_count` is a denormalised cache maintained by a trigger rather
than by the application, for two reasons:

1. **Correctness under concurrency.** `UPDATE issues SET upvote_count =
   upvote_count + 1` inside the trigger is a read-modify-write performed by the
   database under a row lock. The application equivalent — SELECT the count,
   then write it back — loses increments whenever two people upvote the same
   issue at the same moment, which on a trending issue is the normal case, not
   the edge case.
2. **It cannot be bypassed.** A future admin tool, a bulk import or a psql
   session that inserts into `upvotes` keeps the counter correct for free.

`GREATEST(upvote_count - 1, 0)` on DELETE is a floor, not a fix: if the counter
is ever wrong it must be rebuilt from `COUNT(*)`, and the floor only stops a bad
value going negative and rendering as "-1 upvotes" in the UI.

The trigger returns NULL because it is an AFTER trigger, where the return value
is discarded.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "012"
down_revision: str | None = "011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


SYNC_FUNCTION = """
CREATE OR REPLACE FUNCTION fn_sync_upvote_count()
RETURNS TRIGGER AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    UPDATE issues SET upvote_count = upvote_count + 1 WHERE id = NEW.issue_id;
  ELSIF TG_OP = 'DELETE' THEN
    UPDATE issues SET upvote_count = GREATEST(upvote_count - 1, 0) WHERE id = OLD.issue_id;
  END IF;
  RETURN NULL;
END;
$$ LANGUAGE plpgsql;
"""

CREATE_TRIGGER = """
CREATE TRIGGER trg_upvote_count
  AFTER INSERT OR DELETE ON upvotes
  FOR EACH ROW EXECUTE FUNCTION fn_sync_upvote_count();
"""


def upgrade() -> None:
    op.create_table(
        "upvotes",
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "issue_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("issues.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )

    # The composite PK leads with user_id, so it cannot serve "who upvoted this
    # issue?" or the recount path. This index does.
    op.create_index("idx_upvotes_issue", "upvotes", ["issue_id"])

    op.execute(SYNC_FUNCTION)
    op.execute(CREATE_TRIGGER)


def downgrade() -> None:
    # Trigger first: it depends on the function, and the function cannot be
    # dropped while a trigger references it.
    op.execute("DROP TRIGGER IF EXISTS trg_upvote_count ON upvotes;")
    op.execute("DROP FUNCTION IF EXISTS fn_sync_upvote_count();")
    op.drop_index("idx_upvotes_issue", table_name="upvotes")
    op.drop_table("upvotes")
