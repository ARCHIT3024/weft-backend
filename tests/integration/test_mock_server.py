"""Smoke tests for the routers that are still Phase 0 mocks.

Verifies the remaining mock router stubs return the JSON shape the contract
promises, without requiring a database connection.

Auth is no longer among them. `POST /v1/auth/{register,login,refresh,logout}`
and `GET /v1/users/me` are real, database-backed endpoints, so the static
assertions that used to live in `TestAuthMock` here have moved to
`tests/integration/test_auth.py`, where they run against Postgres. The two
OAuth stubs survived the pivot and are covered there too
(`TestOAuthStillMocked`), next to the behaviour that replaced their neighbours.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app


@pytest.fixture
async def client() -> AsyncClient:
    """Test client that hits mock routes directly (no DB needed)."""
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac


# ── Health ───────────────────────────────────────────────────────────────


class TestHealth:
    async def test_health_endpoint(self, client: AsyncClient) -> None:
        response = await client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert "version" in data
        assert "env" in data


# ── Auth Routes That Are Still Stubs ─────────────────────────────────────


class TestAuthMock:
    """Only the auth routes the MVP left mocked.

    Google and Apple sign-in were cut from the MVP but their stubs survive, so
    their shape is still worth pinning here — these need no database, unlike the
    real auth tests.

    `GET /auth/me` used to be pinned alongside them. It was a duplicate of the
    real, implemented `GET /users/me`, which is what `openapi.yaml` specifies;
    the route and this test were deleted together rather than left to drift.
    """

    async def test_google_oauth(self, client: AsyncClient) -> None:
        response = await client.post("/v1/auth/oauth/google")
        assert response.status_code == 200
        assert "access_token" in response.json()

    async def test_apple_oauth(self, client: AsyncClient) -> None:
        response = await client.post("/v1/auth/oauth/apple")
        assert response.status_code == 200
        assert "access_token" in response.json()


# ── Issues Mock Routes ───────────────────────────────────────────────────


# ── Issues and upvotes are no longer mocks ──────────────────────────────
#
# `TestIssuesMock` and `TestUpvotesMock` used to live here, pinning static JSON
# from the Phase 0 stubs. Both were removed when the real handlers landed: the
# endpoints now hit PostGIS, enforce the status machine and require
# authentication, so tests asserting a fixed dictionary were pinning behaviour
# that is gone.
#
# They are replaced by `tests/integration/test_issues.py`, which exercises the
# real lifecycle against a live database. `GET /issues` is covered there too.


# ── Notifications and /users/me are no longer mocks ─────────────────────
#
# `TestNotificationsMock` used to live here. It was removed when the real
# handlers landed (tasks 1.25, 1.26): every notification route now requires an
# access token and reads the caller's own rows from Postgres, so an
# unauthenticated call is a 401 and the fixed notification it asserted no
# longer exists. Replaced by `tests/integration/test_notifications.py`.
# `PATCH /users/me` and `GET /users/me/reports` likewise left their stubs
# behind (tasks 1.10, 2.28) and are covered by
# `tests/integration/test_users_me.py`. `GET /users/leaderboard` is still a
# stub (task 4.3) and has never been pinned here.


# ── Analytics is no longer a mock ────────────────────────────────────────
#
# `TestAnalyticsMock` used to live here, asserting the fixed numbers of the
# Phase 0 stubs. It was removed when the real handlers landed (task 1.29, plus
# SLA breaches and CSV export): every analytics route now requires an AUTHORITY
# or ADMIN token and aggregates live over Postgres, so an unauthenticated call
# is a 401. Replaced by `tests/integration/test_analytics.py`.


# ── Admin is no longer a mock ────────────────────────────────────────────
#
# `TestAdminMock` used to live here. It was removed when the real handlers
# landed (tasks 1.11, 1.12, 4.15a): every admin route now requires an ADMIN
# token and writes to Postgres, so an unauthenticated call is a 401 and the
# fixed dictionaries it asserted no longer exist. Replaced by
# `tests/integration/test_admin.py`.
