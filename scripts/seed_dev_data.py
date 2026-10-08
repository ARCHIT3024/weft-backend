"""Seed local development data: zones, demo accounts and an authority posting.

    python -m scripts.seed_dev_data          # seed
    python -m scripts.seed_dev_data --drop   # remove everything this script made

**DEVELOPMENT ONLY. This must never run against staging or production.**

That is why this is a script and not an Alembic migration. Migration 014 seeds
departments and the category routing map, because that is operational
configuration production genuinely needs. Everything *here* is fake: accounts
with passwords written in this file, and a zone polygon drawn around a demo
city. A migration is the thing that gets run everywhere by definition, so a
known-password admin account inside one is a production security hole waiting
for the first person who runs `alembic upgrade head` against a real database.

As a second line of defence the script refuses to run when `ENV` is anything
other than `development` / `local` / `test`, and refuses when `DATABASE_URL`
does not point at localhost. Both are overridable with `--force`, deliberately
awkwardly, because someone doing it by accident should have to type something
they would notice.

Idempotent: re-running updates the demo rows in place rather than duplicating
them, so it is safe to run repeatedly while developing.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from dataclasses import dataclass

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.core.security import hash_password
from app.models.authority_user import AuthorityUser
from app.models.authority_zone import AuthorityZone
from app.models.department import Department
from app.models.user import User
from app.models.zone import Zone

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("seed")

SAFE_ENVS = {"development", "local", "test", "dev"}

# Bengaluru — matches the ap-south-1 / Indian-municipality target in
# DECISIONS.md. Two adjacent rectangles so zone assignment is actually
# exercised: an issue can land in one, the other, or neither.
DEMO_ZONES: tuple[tuple[str, str], ...] = (
    (
        "Central Zone",
        "POLYGON((77.5700 12.9500, 77.6100 12.9500, 77.6100 12.9900, 77.5700 12.9900, 77.5700 12.9500))",
    ),
    (
        "North Zone",
        "POLYGON((77.5700 12.9900, 77.6100 12.9900, 77.6100 13.0300, 77.5700 13.0300, 77.5700 12.9900))",
    ),
)


@dataclass(frozen=True)
class DemoAccount:
    email: str
    name: str
    password: str
    role: str


DEMO_ACCOUNTS: tuple[DemoAccount, ...] = (
    DemoAccount("admin@weft.local", "Demo Admin", "DevPassword123!", "ADMIN"),
    DemoAccount("authority@weft.local", "Demo Authority Officer", "DevPassword123!", "AUTHORITY"),
    DemoAccount("citizen@weft.local", "Demo Citizen", "DevPassword123!", "CITIZEN"),
)

# The authority account is posted to this department and given both zones.
AUTHORITY_DEPARTMENT_CODE = "PWD"
AUTHORITY_EMPLOYEE_ID = "DEV-0001"


def _guard(force: bool) -> None:
    """Refuse to touch anything that might not be a developer's own machine."""
    problems: list[str] = []

    if settings.ENV.lower() not in SAFE_ENVS:
        problems.append(f"ENV is {settings.ENV!r}, expected one of {sorted(SAFE_ENVS)}")

    url = settings.DATABASE_URL
    if not any(host in url for host in ("localhost", "127.0.0.1", "@db:", "@db/")):
        # Printed without credentials — the URL carries a password.
        problems.append("DATABASE_URL does not look local")

    if problems and not force:
        logger.error("Refusing to seed demo data:")
        for problem in problems:
            logger.error("  - %s", problem)
        logger.error("")
        logger.error("This script creates accounts whose passwords are written in its source.")
        logger.error("If you are certain this is a development database, re-run with --force.")
        sys.exit(1)

    if problems:
        logger.warning("--force given; proceeding despite: %s", "; ".join(problems))


async def _seed_zones(db: AsyncSession, department_id: uuid.UUID | None) -> list[Zone]:
    zones: list[Zone] = []
    for name, polygon_wkt in DEMO_ZONES:
        # `boundary` is NOT NULL, so the geometry must be present at INSERT —
        # it cannot be added by a follow-up UPDATE. GeoAlchemy2 accepts EWKT,
        # and the explicit SRID prefix is what makes the value match the
        # column's 4326 rather than landing as SRID 0.
        ewkt = f"SRID=4326;{polygon_wkt}"
        zone = await db.scalar(select(Zone).where(Zone.name == name))
        if zone is None:
            zone = Zone(name=name, department_id=department_id, is_active=True, boundary=ewkt)
            db.add(zone)
            await db.flush()
        else:
            # Refresh in place so an edited polygon in this file takes effect.
            await db.execute(
                text("UPDATE zones SET boundary = ST_GeomFromEWKT(:ewkt) WHERE id = :zone_id"),
                {"ewkt": ewkt, "zone_id": zone.id},
            )
        zones.append(zone)
        logger.info("  zone: %s", name)
    return zones


async def _seed_accounts(db: AsyncSession) -> dict[str, User]:
    users: dict[str, User] = {}
    for account in DEMO_ACCOUNTS:
        user = await db.scalar(select(User).where(User.email == account.email))
        if user is None:
            user = User(email=account.email, name=account.name, is_anonymous=False)
            db.add(user)
        # Rewritten every run so a changed password in this file actually takes
        # effect, and so a locally deactivated demo account comes back.
        user.name = account.name
        user.password_hash = hash_password(account.password)
        user.role = account.role
        user.is_active = True
        await db.flush()
        users[account.role] = user
        logger.info("  account: %-24s %s", account.email, account.role)
    return users


async def _seed_authority_profile(db: AsyncSession, user: User, zones: list[Zone]) -> None:
    department = await db.scalar(select(Department).where(Department.code == AUTHORITY_DEPARTMENT_CODE))
    if department is None:
        logger.error(
            "  department %s not found — run `alembic upgrade head` first (migration 014 seeds it)",
            AUTHORITY_DEPARTMENT_CODE,
        )
        return

    profile = await db.scalar(select(AuthorityUser).where(AuthorityUser.user_id == user.id))
    if profile is None:
        profile = AuthorityUser(
            user_id=user.id,
            department_id=department.id,
            employee_id=AUTHORITY_EMPLOYEE_ID,
            designation="Junior Engineer",
            is_dept_admin=True,
        )
        db.add(profile)
        await db.flush()
    logger.info("  authority profile: %s @ %s", AUTHORITY_EMPLOYEE_ID, department.code)

    for zone in zones:
        link = await db.scalar(
            select(AuthorityZone).where(
                AuthorityZone.authority_user_id == profile.id,
                AuthorityZone.zone_id == zone.id,
            )
        )
        if link is None:
            db.add(AuthorityZone(authority_user_id=profile.id, zone_id=zone.id))
    await db.flush()
    logger.info("  authority zones: %s", ", ".join(z.name for z in zones))


async def _drop(db: AsyncSession) -> None:
    """Remove exactly what this script creates, in FK-safe order."""
    emails = [a.email for a in DEMO_ACCOUNTS]
    zone_names = [name for name, _ in DEMO_ZONES]

    users = (await db.scalars(select(User).where(User.email.in_(emails)))).all()
    profiles = (await db.scalars(select(AuthorityUser).where(AuthorityUser.user_id.in_([u.id for u in users])))).all()

    if profiles:
        await db.execute(delete(AuthorityZone).where(AuthorityZone.authority_user_id.in_([p.id for p in profiles])))
        await db.execute(delete(AuthorityUser).where(AuthorityUser.id.in_([p.id for p in profiles])))
    if users:
        await db.execute(delete(User).where(User.id.in_([u.id for u in users])))
    await db.execute(delete(Zone).where(Zone.name.in_(zone_names)))
    logger.info("removed %d demo account(s) and %d zone(s)", len(users), len(zone_names))


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--drop", action="store_true", help="remove the demo data instead of creating it")
    parser.add_argument("--force", action="store_true", help="bypass the development-environment guard")
    args = parser.parse_args()

    _guard(args.force)

    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with session_factory() as db:
            if args.drop:
                await _drop(db)
            else:
                logger.info("seeding development data")
                department = await db.scalar(select(Department).where(Department.code == AUTHORITY_DEPARTMENT_CODE))
                zones = await _seed_zones(db, department.id if department else None)
                users = await _seed_accounts(db)
                if "AUTHORITY" in users:
                    await _seed_authority_profile(db, users["AUTHORITY"], zones)
            await db.commit()
    finally:
        await engine.dispose()

    if not args.drop:
        logger.info("")
        logger.info("Demo logins (development only):")
        for account in DEMO_ACCOUNTS:
            logger.info("  %-24s %s   [%s]", account.email, account.password, account.role)


if __name__ == "__main__":
    asyncio.run(main())
