"""Shared pytest fixtures for Weft backend tests.

Provides:
- Async test database session with transaction rollback isolation
- AsyncClient for integration testing against the FastAPI app
- Auth token fixtures for citizen, authority, and admin roles
- Redis mock fixture

Per TRD Section 9:
- Test DB is separate (TEST_DATABASE_URL)
- Each test runs in a transaction that is rolled back
- External services (S3, FCM, Rekognition) are mocked
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings
from app.core.security import create_access_token
from app.database import get_db
from app.main import create_app

logger = logging.getLogger(__name__)

# Backend root (the directory holding alembic.ini), resolved from this file so
# the fixture works no matter which directory pytest was invoked from.
BACKEND_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"

# The probe answers exactly one question: can we obtain a usable connection to
# the test database? Every failure mode answers "no" — refused connection, no
# Docker, unresolvable host, wrong port, bad credentials, missing database — so
# the probe catches broadly and skips rather than enumerating error classes.
#
# An earlier narrow tuple (OSError, TimeoutError, sqlalchemy DBAPIError) missed
# asyncpg.exceptions.InvalidPasswordError, which arrives unwrapped when some
# *other* Postgres is already listening on 5432. Because this fixture is
# session-scoped and autouse, that turned every pure-unit test into an error on
# any machine with an unrelated local Postgres.
#
# This deliberately does NOT weaken the important distinction: migrations run
# after the probe, outside any except block, so a broken migration still fails
# the run loudly instead of masquerading as an absent database.
DB_PROBE_FAILURES = Exception

# Seconds to wait for the connectivity probe before declaring the DB absent.
DB_PROBE_TIMEOUT_SECONDS = 10.0


# ── Event Loop ──────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def event_loop():
    """Create a single event loop for the entire test session."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ── Test Database Engine ────────────────────────────────────────────────

test_engine = create_async_engine(
    settings.TEST_DATABASE_URL,
    pool_size=5,
    max_overflow=5,
    echo=False,
)

TestSessionFactory = async_sessionmaker(
    test_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


def _alembic_config() -> Config:
    """Build an Alembic Config pointed at the *test* database.

    The URL is passed through ``config.attributes``, which app/migrations/env.py
    prefers over settings.DATABASE_URL. Setting only ``sqlalchemy.url`` would be
    silently overridden by env.py and would migrate (and then drop!) the
    developer's dev database instead.
    """
    cfg = Config(str(ALEMBIC_INI))
    # alembic.ini's script_location is relative to the ini file; make it absolute
    # so the fixture is independent of the process working directory.
    cfg.set_main_option("script_location", str(BACKEND_ROOT / "app" / "migrations"))
    cfg.attributes["sqlalchemy_url"] = settings.TEST_DATABASE_URL
    return cfg


async def _test_db_is_reachable() -> bool:
    """Return True if the test database accepts connections.

    This is deliberately separate from running migrations: a refused connection
    means "no database in this environment, skip", whereas a migration error
    means "the migrations are broken" and must never be mistaken for the former.
    """

    async def _probe() -> None:
        async with test_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    try:
        # The timeout wraps the connect too, so a filtered port cannot hang the
        # session for the OS-level TCP timeout.
        await asyncio.wait_for(_probe(), DB_PROBE_TIMEOUT_SECONDS)
    except DB_PROBE_FAILURES as exc:
        logger.warning(
            "Test database %s is not reachable (%s: %s). DB-backed tests will run without "
            "a migrated schema; start Postgres (docker compose up -d db) to exercise them.",
            settings.TEST_DATABASE_URL,
            type(exc).__name__,
            exc,
        )
        return False
    return True


@pytest.fixture(scope="session", autouse=True)
async def _setup_test_db() -> AsyncGenerator[None, None]:
    """Apply Alembic migrations to the test DB for the session; unwind at the end.

    Base.metadata.create_all() cannot be used here: every Postgres enum column is
    declared with ``create_type=False`` because migration 002 owns enum creation,
    so create_all() against a fresh database fails on the missing types. The
    schema under test must therefore come from the same migrations CI and
    production run — `alembic upgrade head`.

    Failure handling is split in two on purpose:

    * Database unreachable (no Docker / no CI service) -> warn and skip, so the
      non-DB tests still run locally.
    * Database reachable but ``upgrade head`` fails -> raise. The upgrade runs
      outside every ``except``, so a broken migration errors the session loudly
      instead of leaving tests to pass against an empty database.

    Alembic is driven via ``asyncio.to_thread`` because env.py's
    run_migrations_online() calls ``asyncio.run()``, which cannot be called from
    the already-running loop this async fixture lives in. The worker thread gets
    its own event loop.
    """
    if not await _test_db_is_reachable():
        yield
        await test_engine.dispose()
        return

    # NOT wrapped in try/except: a migration failure must surface as an error.
    await asyncio.to_thread(command.upgrade, _alembic_config(), "head")

    yield

    try:
        await asyncio.to_thread(command.downgrade, _alembic_config(), "base")
    except Exception:
        # Teardown only — logged rather than raised so a cleanup failure cannot
        # mask the results of the tests that just ran. Never silently passed.
        logger.exception("Failed to downgrade the test database to base; it may be left dirty.")
    finally:
        await test_engine.dispose()


@pytest.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """Provide a transactional database session.

    Each test gets its own transaction that is rolled back at the end,
    ensuring full test isolation without table truncation.
    """
    async with test_engine.connect() as conn:
        transaction = await conn.begin()
        session = AsyncSession(bind=conn, expire_on_commit=False)

        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()


# ── FastAPI Test Client ─────────────────────────────────────────────────


@pytest.fixture
async def app(db_session: AsyncSession):
    """Create a FastAPI app instance with the test DB session injected."""
    test_app = create_app()

    async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    test_app.dependency_overrides[get_db] = _override_get_db
    return test_app


@pytest.fixture
async def client(app) -> AsyncGenerator[AsyncClient, None]:
    """Async HTTP test client for integration tests."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac


# ── Auth Token Fixtures ─────────────────────────────────────────────────


@pytest.fixture
def auth_token_citizen() -> str:
    """JWT access token for a citizen user (for testing authenticated endpoints)."""
    return create_access_token(
        user_id="00000000-0000-0000-0000-000000000001",
        role="CITIZEN",
        email="citizen@test.com",
    )


@pytest.fixture
def auth_token_authority() -> str:
    """JWT access token for an authority user."""
    return create_access_token(
        user_id="00000000-0000-0000-0000-000000000002",
        role="AUTHORITY",
        email="authority@test.com",
    )


@pytest.fixture
def auth_token_admin() -> str:
    """JWT access token for an admin user."""
    return create_access_token(
        user_id="00000000-0000-0000-0000-000000000003",
        role="ADMIN",
        email="admin@test.com",
    )


# ── External Service Mocks ──────────────────────────────────────────────


@pytest.fixture
def mock_s3_client():
    """Mock boto3 S3 client for image upload tests."""
    with patch("boto3.client") as mock:
        s3 = AsyncMock()
        mock.return_value = s3
        yield s3


@pytest.fixture
def mock_redis():
    """Mock Redis client."""
    redis = AsyncMock()
    redis.pipeline.return_value = AsyncMock()
    yield redis
