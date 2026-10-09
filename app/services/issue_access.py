"""Who may act on which issue — the authority jurisdiction rule, in one place.

TRD §RBAC grants an AUTHORITY "View all issues in zone". The rule, which is the
one `analytics_service` states for reporting (its module docstring, "Scope",
and `_scope_for`):

* An **ADMIN** may act on every issue.
* An **AUTHORITY** may act only on issues whose `zone_id` is one of their
  `authority_zones`. Their department is *not* a boundary — a supervisor's
  jurisdiction is geographic.
* An issue outside every zone (`zone_id IS NULL`) is therefore **ADMIN-only**.
* Any other role, and an AUTHORITY with no `authority_users` profile, has an
  empty jurisdiction.

The public reads (`GET /issues`, `/issues/nearby`, `/issues/{id}`) are not
scoped: the citizen map needs them. This governs the triage *writes* and the
staff reads that hang off them.

**Out of jurisdiction is the same 404 as a missing issue** — `NotFoundError
("Issue")`, byte for byte — so a triage endpoint cannot be used to learn that an
issue id exists in somebody else's zone. A 403 would answer exactly that.

One deliberate difference from analytics: there, an AUTHORITY with no profile
is refused with 403 `AUTHORITY_PROFILE_MISSING`, because answering a KPI query
with zeros would disguise a provisioning fault as a quiet municipality. Here
the same account simply has no zones and every issue is a 404 to it; a
distinct error would again confirm the issue exists. `analytics_service` could
move onto `zone_scope` — its `_scope_for` is the same lookup plus that 403 —
so the two can never disagree about who sees what.

**Assignment follows visibility.** An issue may be assigned only to someone who
could act on it themselves: an active staff account whose own jurisdiction, by
this same rule, covers the issue. `eligible_assignees` is that set, and both
the staff picker and the assign check read it, so the picker can never offer a
name the assign endpoint would refuse.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Select, and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import Role
from app.models.authority_user import AuthorityUser
from app.models.authority_zone import AuthorityZone
from app.models.department import Department
from app.models.user import User


async def zone_scope(db: AsyncSession, user: User) -> frozenset[uuid.UUID] | None:
    """None for an ADMIN (unrestricted); the zones an AUTHORITY covers otherwise.

    Read by query, not via `user.authority_profile`: the user may be an object
    this session loaded without that relationship, and touching an unloaded
    relationship under asyncio raises rather than lazy-loading.
    """
    if user.role == Role.ADMIN:
        return None
    if user.role != Role.AUTHORITY:
        return frozenset()

    zone_ids = await db.scalars(
        select(AuthorityZone.zone_id)
        .join(AuthorityUser, AuthorityUser.id == AuthorityZone.authority_user_id)
        .where(AuthorityUser.user_id == user.id)
    )
    return frozenset(zone_ids.all())


def in_scope(scope: frozenset[uuid.UUID] | None, zone_id: uuid.UUID | None) -> bool:
    """Whether an issue in `zone_id` falls inside `scope` (see `zone_scope`).

    A NULL zone is inside no authority's scope — only the unrestricted one.
    """
    if scope is None:
        return True
    return zone_id is not None and zone_id in scope


async def can_act_on(db: AsyncSession, user: User, zone_id: uuid.UUID | None) -> bool:
    """Whether `user` may change the status of, or assign, an issue in `zone_id`."""
    return in_scope(await zone_scope(db, user), zone_id)


def eligible_assignees(zone_id: uuid.UUID | None) -> Select:
    """Active staff who could act on an issue in `zone_id`, as minimal columns.

    The same rule as `zone_scope`, expressed in SQL over every staff profile at
    once: an active ADMIN with an authority profile covers everything; an
    active AUTHORITY covers the zones linked to their profile; nobody else is
    eligible. A NULL zone is therefore assignable to admins only.

    Columns, not ORM entities, on the pattern of
    `admin_service._authority_columns`: the picker is open to every authority,
    not only admins, so it exposes the least that identifies a colleague — no
    email, no phone, no employee id. Ordered by name, then id, so the list reads
    alphabetically and is stable when two people share a name.
    """
    staff_rule = User.role == Role.ADMIN.value
    if zone_id is not None:
        covers_zone = exists().where(
            AuthorityZone.authority_user_id == AuthorityUser.id,
            AuthorityZone.zone_id == zone_id,
        )
        staff_rule = or_(staff_rule, and_(User.role == Role.AUTHORITY.value, covers_zone))

    return (
        select(
            AuthorityUser.id,
            User.name,
            AuthorityUser.designation,
            Department.name.label("department_name"),
        )
        .join(User, User.id == AuthorityUser.user_id)
        .join(Department, Department.id == AuthorityUser.department_id)
        .where(User.is_active.is_(True), staff_rule)
        .order_by(User.name, AuthorityUser.id)
    )
