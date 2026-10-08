"""Notifications router — the caller's in-app notification centre.

Every route acts on the authenticated caller's own notifications and nothing
else; no route takes a user id. Rows are written by
`app.services.notification_service.deliver`, never through this router.

`/read-all` and `/{notification_id}/read` cannot shadow each other — one is a
single path segment, the other two — so declaration order does not matter
here, unlike `/issues/nearby`.
"""

from __future__ import annotations

import logging
import math
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.core.permissions import get_current_user
from app.dependencies import DBSession
from app.models.user import User
from app.schemas.notification import (
    MarkAllNotificationsReadResponse,
    NotificationListResponse,
    NotificationOut,
)
from app.services import notification_service

logger = logging.getLogger(__name__)

router = APIRouter()

CurrentUser = Annotated[User, Depends(get_current_user)]


@router.get("", response_model=NotificationListResponse, summary="List own notifications")
async def list_notifications(
    db: DBSession,
    current_user: CurrentUser,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    unread_only: bool = False,
) -> NotificationListResponse:
    """The caller's notifications, newest first, with the unread badge count."""
    rows, total, unread_count = await notification_service.list_notifications(
        db,
        user_id=current_user.id,
        page=page,
        page_size=page_size,
        unread_only=unread_only,
    )
    return NotificationListResponse(
        items=[NotificationOut.model_validate(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
        # Same rule as `PaginatedResponse.create`: an empty result is one page.
        total_pages=max(1, math.ceil(total / page_size)),
        unread_count=unread_count,
    )


@router.patch("/read-all", response_model=MarkAllNotificationsReadResponse, summary="Mark all as read")
async def mark_all_read(db: DBSession, current_user: CurrentUser) -> MarkAllNotificationsReadResponse:
    """Mark every unread notification of the caller read. Idempotent."""
    updated = await notification_service.mark_all_read(db, user_id=current_user.id)
    return MarkAllNotificationsReadResponse(updated_count=updated)


@router.patch("/{notification_id}/read", response_model=NotificationOut, summary="Mark one as read")
async def mark_read(db: DBSession, current_user: CurrentUser, notification_id: uuid.UUID) -> NotificationOut:
    """Mark one notification read. Idempotent.

    Someone else's notification is a **404, not a 403**: a 403 would confirm the
    id exists, letting a caller probe for valid ids one request at a time.
    """
    notification = await notification_service.mark_read(
        db,
        user_id=current_user.id,
        notification_id=notification_id,
    )
    return NotificationOut.model_validate(notification)
