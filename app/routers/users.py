"""Users router — the caller's own profile, devices and reports.

`GET /users/me`, `PATCH /users/me` and `GET /users/me/reports` are real.
`GET /users/leaderboard` is still a Phase 0 mock: gamification is task 4.3.

Every `/me` route acts on the authenticated caller and takes no user id, so
there is no parameter through which one account could read or edit another.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.core.permissions import get_current_user
from app.dependencies import DBSession
from app.models.user import User
from app.schemas.common import PaginatedResponse
from app.schemas.issue import IssueStatus, IssueSummary
from app.schemas.user import UpdateProfileRequest, UserProfile
from app.services import issue_service, user_service

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


@router.patch("/me", response_model=UserProfile, summary="Update own profile / register push device")
async def update_my_profile(db: DBSession, current_user: CurrentUser, payload: UpdateProfileRequest) -> UserProfile:
    """Partial update of the caller's profile; returns the updated profile.

    `fcm_token` registers this device for push. The mobile app sends it on every
    launch (task 2.31), which also keeps `last_seen` fresh. A token another
    account held moves to the caller — a phone has one current user.

    The token is write-only: no response ever echoes it back.
    """
    user = await user_service.update_profile(
        db,
        user=current_user,
        name=payload.name,
        preferred_lang=payload.preferred_lang.value if payload.preferred_lang else None,
        fcm_token=payload.fcm_token,
    )
    return UserProfile.model_validate(user)


@router.get("/me/reports", response_model=PaginatedResponse[IssueSummary], summary="List own submitted issues")
async def get_my_reports(
    db: DBSession,
    current_user: CurrentUser,
    issue_status: Annotated[list[IssueStatus] | None, Query(alias="status")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> PaginatedResponse[IssueSummary]:
    """The caller's own submitted issues, newest first.

    Same query and same item shape as `GET /issues`, narrowed by reporter. The
    reporter filter is the caller's id, never a parameter. Anonymous reports
    have no reporter and so appear in nobody's list — including the list of
    whoever filed them, which is what anonymous means.
    """
    rows, total = await issue_service.list_issues(
        db,
        filters=issue_service.IssueFilters(
            reporter_id=current_user.id,
            statuses=[s.value for s in issue_status] if issue_status else None,
        ),
        page=page,
        page_size=page_size,
        sort_field="created_at",
        descending=True,
    )
    return PaginatedResponse.create(
        items=[IssueSummary.model_validate(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


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
