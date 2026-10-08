"""Users router — `GET /users/me` is real; the rest are Phase 0 mock stubs."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.permissions import get_current_user
from app.models.user import User
from app.schemas.user import UserProfile

logger = logging.getLogger(__name__)

router = APIRouter()

CurrentUser = Annotated[User, Depends(get_current_user)]


@router.get("/me", response_model=UserProfile, summary="Get own profile + trust score")
async def get_my_profile(current_user: CurrentUser) -> UserProfile:
    """Return the caller's own profile.

    `get_current_user` does the work: it validates the RS256 signature *and*
    re-checks `users.is_active`, so a cryptographically valid token belonging to
    a deactivated account is a 401 rather than a live session.

    `UserProfile` carries no credential material — no password hash, no tokens.
    """
    return UserProfile.model_validate(current_user)


@router.patch("/me")
async def update_profile_mock() -> dict:
    """Mock: Update profile (name, lang, FCM token)."""
    return {
        "id": "550e8400-e29b-41d4-a716-446655440001",
        "email": "citizen@example.com",
        "name": "Updated Name",
        "role": "CITIZEN",
        "preferred_lang": "hi",
    }


@router.get("/me/reports")
async def get_my_reports_mock() -> dict:
    """Mock: Get own submitted issues."""
    return {
        "items": [],
        "total": 0,
        "page": 1,
        "page_size": 20,
        "total_pages": 1,
    }


@router.get("/leaderboard")
async def get_leaderboard_mock() -> dict:
    """Mock: Monthly top-contributor leaderboard."""
    return {
        "month": "2026-07",
        "entries": [
            {"rank": 1, "user_id": "uuid-1", "name": "Top Citizen", "total_points": 250, "title": "City Guardian"},
            {
                "rank": 2,
                "user_id": "uuid-2",
                "name": "Second Citizen",
                "total_points": 180,
                "title": "Civic Contributor",
            },
        ],
    }
