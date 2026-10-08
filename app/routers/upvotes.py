"""Upvotes router — community priority signal on an issue.

Authenticated citizens only. Anonymous upvoting is deliberately not offered:
an upvote is a prioritisation signal that municipal staff sort by, and an
unauthenticated one is a counter anybody can inflate without limit.

Neither handler touches `issues.upvote_count`. That column is owned by the
`trg_upvote_count` database trigger (migration 012) — see
`app/services/issue_service.py` for why the counter is maintained there rather
than in Python.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.core.permissions import get_current_user
from app.dependencies import DBSession
from app.models.user import User
from app.schemas.issue import UpvoteResponse
from app.services import issue_service

logger = logging.getLogger(__name__)

router = APIRouter()

CurrentUser = Annotated[User, Depends(get_current_user)]


@router.post(
    "/{issue_id}/upvote",
    response_model=UpvoteResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upvote an issue",
)
async def upvote_issue(db: DBSession, issue_id: uuid.UUID, current_user: CurrentUser) -> UpvoteResponse:
    """Record this citizen's upvote.

    A second upvote from the same account is a `409`, enforced by the
    `(user_id, issue_id)` primary key rather than by a prior lookup — two
    concurrent requests both passing a "have they voted?" check is precisely
    the race that produces a double count.
    """
    issue = await issue_service.add_upvote(db, issue_id=issue_id, user=current_user)
    upvoted_at = await issue_service.upvote_timestamp(db, issue_id=issue_id, user_id=current_user.id)

    return UpvoteResponse(
        issue_id=issue.id,
        user_id=current_user.id,
        upvote_count=issue.upvote_count,
        created_at=upvoted_at,
    )


@router.delete(
    "/{issue_id}/upvote",
    status_code=status.HTTP_204_NO_CONTENT,
    # Required, not cosmetic: with `from __future__ import annotations` FastAPI
    # resolves the `-> None` return annotation to `NoneType` and asserts at
    # import time that a 204 must not have a body. See D-6.
    response_model=None,
    summary="Withdraw an upvote",
)
async def remove_upvote(db: DBSession, issue_id: uuid.UUID, current_user: CurrentUser) -> None:
    """Withdraw this citizen's upvote.

    Idempotent: withdrawing an upvote that was never cast returns 204 too. The
    caller's intent — "I should not be counted" — is satisfied either way, and a
    404 would leak whether a given user had upvoted a given issue.
    """
    await issue_service.remove_upvote(db, issue_id=issue_id, user=current_user)
