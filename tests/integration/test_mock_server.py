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
#
# The classes below still pin genuine mocks — notifications, analytics and
# admin are unimplemented, and these tests are what keeps their contract
# surface from drifting before the real handlers arrive.


class TestNotificationsMock:
    async def test_list_notifications(self, client: AsyncClient) -> None:
        response = await client.get("/v1/notifications")
        assert response.status_code == 200
        data = response.json()
        assert "items" in data

    async def test_mark_read(self, client: AsyncClient) -> None:
        response = await client.patch("/v1/notifications/some-id/read")
        assert response.status_code == 200

    async def test_mark_all_read(self, client: AsyncClient) -> None:
        response = await client.patch("/v1/notifications/read-all")
        assert response.status_code == 200


# ── Analytics Mock Routes ────────────────────────────────────────────────


class TestAnalyticsMock:
    async def test_summary(self, client: AsyncClient) -> None:
        response = await client.get("/v1/analytics/summary")
        assert response.status_code == 200
        data = response.json()
        assert "total_reported" in data
        assert "total_resolved" in data

    async def test_heatmap(self, client: AsyncClient) -> None:
        response = await client.get("/v1/analytics/heatmap")
        assert response.status_code == 200
        assert "points" in response.json()

    async def test_resolution_times(self, client: AsyncClient) -> None:
        response = await client.get("/v1/analytics/resolution-times")
        assert response.status_code == 200


# ── Admin Mock Routes ────────────────────────────────────────────────────


class TestAdminMock:
    async def test_create_authority(self, client: AsyncClient) -> None:
        response = await client.post("/v1/admin/authority-users")
        assert response.status_code == 201
        assert response.json()["role"] == "AUTHORITY"

    async def test_deactivate_authority(self, client: AsyncClient) -> None:
        response = await client.patch("/v1/admin/authority-users/some-uuid/deactivate")
        assert response.status_code == 200
        assert response.json()["is_active"] is False

    async def test_list_departments(self, client: AsyncClient) -> None:
        response = await client.get("/v1/admin/departments")
        assert response.status_code == 200

    async def test_create_department(self, client: AsyncClient) -> None:
        response = await client.post("/v1/admin/departments")
        assert response.status_code == 201

    async def test_create_zone(self, client: AsyncClient) -> None:
        response = await client.post("/v1/admin/zones")
        assert response.status_code == 201
