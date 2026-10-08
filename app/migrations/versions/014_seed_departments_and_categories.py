"""Seed departments + department_categories routing map

Revision ID: 014
Revises: 013
Create Date: 2026-09-08

Task 1.13 (planned as migration 018; lands here because 009-013 were taken by
the issues data layer — the number moved, the content did not).

**This migration seeds reference data only — no user accounts.** Every row here
is operational configuration that production genuinely needs: without a
`department_categories` row for a category, `POST /issues` has nothing to route
that category to. Demo accounts with known passwords live in
`scripts/seed_dev_data.py` instead, which never runs against production. A
migration that creates a known-password admin is a production security hole,
and migrations are exactly the thing that gets run everywhere.

The mapping covers all seven `issue_category` values exactly once, so routing is
total: every category resolves to a department.

**It is not, however, structurally unambiguous.** `department_categories` has a
*composite* primary key `(department_id, category)`, so the schema permits the
same category to be mapped to several departments — deliberate design space per
`05_weft_backend_schema.md`, since a municipality may split a category across
departments. This seed maps each category once, but nothing in the database
enforces that. **Any routing lookup must therefore be deterministic** (order the
result, do not take an arbitrary first row), or the department an issue is
routed to could vary between identical submissions once an operator adds a
second mapping. If one-department-per-category is meant to be an invariant, it
needs a `UNIQUE (category)` constraint and a recorded decision — this migration
does not add one, because that would contradict the schema doc.

Idempotent: `ON CONFLICT DO NOTHING` on both inserts, so re-running against a
partially seeded database is safe.

`sla_hours` values are per-department starting points, not policy. They are
editable per department by task 4.15a; treat them as defaults to be tuned with
the pilot municipality, not as commitments.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "014"
down_revision: str | None = "013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# (code, name, description, sla_hours, upvote_alert_threshold)
DEPARTMENTS: tuple[tuple[str, str, str, int, int], ...] = (
    ("PWD", "Public Works Department", "Roads, footpaths and civil infrastructure", 72, 10),
    ("SAN", "Sanitation Department", "Solid waste collection and street cleaning", 24, 15),
    ("WSD", "Water & Sewerage Department", "Water supply, drainage and sewerage", 48, 10),
    ("ELE", "Electrical Department", "Street lighting and public electrical assets", 48, 10),
    ("PRK", "Parks & Horticulture", "Public parks, trees and green spaces", 96, 5),
    ("GEN", "General Administration", "Fallback for uncategorised reports", 120, 20),
)

# category -> department code. Covers all 7 issue_category values exactly once.
CATEGORY_ROUTING: tuple[tuple[str, str], ...] = (
    ("POTHOLE", "PWD"),
    ("DAMAGED_FOOTPATH", "PWD"),
    ("GARBAGE_ACCUMULATION", "SAN"),
    ("WATER_LOGGING", "WSD"),
    ("SEWAGE_OVERFLOW", "WSD"),
    ("BROKEN_STREET_LIGHT", "ELE"),
    # OTHER is the catch-all the citizen picks when nothing fits, so it routes
    # to General Administration for manual triage rather than being guessed at.
    ("OTHER", "GEN"),
)


def upgrade() -> None:
    conn = op.get_bind()

    for code, name, description, sla_hours, threshold in DEPARTMENTS:
        conn.execute(
            sa.text(
                """
                INSERT INTO departments
                    (id, code, name, description, sla_hours, upvote_alert_threshold, is_active)
                VALUES
                    (gen_random_uuid(), :code, :name, :description, :sla_hours, :threshold, TRUE)
                ON CONFLICT (code) DO NOTHING
                """
            ),
            {
                "code": code,
                "name": name,
                "description": description,
                "sla_hours": sla_hours,
                "threshold": threshold,
            },
        )

    for category, dept_code in CATEGORY_ROUTING:
        conn.execute(
            sa.text(
                """
                INSERT INTO department_categories (department_id, category)
                SELECT d.id, CAST(:category AS issue_category)
                  FROM departments d
                 WHERE d.code = :dept_code
                ON CONFLICT (department_id, category) DO NOTHING
                """
            ),
            {"category": category, "dept_code": dept_code},
        )


def downgrade() -> None:
    conn = op.get_bind()

    # Routing rows first — they reference departments.
    conn.execute(
        sa.text("DELETE FROM department_categories WHERE category = ANY(CAST(:cats AS issue_category[]))"),
        {"cats": [c for c, _ in CATEGORY_ROUTING]},
    )
    # Only remove departments still untouched by real data. A department that
    # has acquired zones, staff or issues is no longer seed data, and silently
    # deleting it would cascade further than a downgrade should reach.
    conn.execute(
        sa.text(
            """
            DELETE FROM departments d
             WHERE d.code = ANY(:codes)
               AND NOT EXISTS (SELECT 1 FROM zones z WHERE z.department_id = d.id)
               AND NOT EXISTS (SELECT 1 FROM authority_users a WHERE a.department_id = d.id)
               AND NOT EXISTS (SELECT 1 FROM issues i WHERE i.department_id = d.id)
            """
        ),
        {"codes": [c for c, _, _, _, _ in DEPARTMENTS]},
    )
