"""Notification schemas — the in-app notification centre.

Derived from the OpenAPI contract (`openapi.yaml`) component schemas
`Notification`, `NotificationType`, `NotificationChannel`,
`NotificationListResponse` and `MarkAllNotificationsReadResponse`, which back
`GET /notifications`, `PATCH /notifications/{id}/read` and
`PATCH /notifications/read-all`.

Field names follow the Phase 0 mock these endpoints replace, so a client built
against the mock keeps working. Two columns are deliberately absent from every
response: `sent_at` and `retry_count` describe FCM delivery bookkeeping, which
is the server's concern, not the reader's — whether a push reached the phone
changes nothing about what the notification centre shows.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import PaginatedResponse

# ── Enums ───────────────────────────────────────────────────────────────


class NotificationType(StrEnum):
    """Why a notification was sent.

    Mirrors `components.schemas.NotificationType` and the `notification_type`
    Postgres enum created in migration 002. All three lists must stay equal.
    Only `STATUS_CHANGE` is emitted today; the others are reserved for the
    features that will emit them (assignment, upvote milestones, gamification).
    """

    STATUS_CHANGE = "STATUS_CHANGE"
    ISSUE_ASSIGNED = "ISSUE_ASSIGNED"
    UPVOTE_MILESTONE = "UPVOTE_MILESTONE"
    GAMIFICATION_REWARD = "GAMIFICATION_REWARD"
    SYSTEM = "SYSTEM"


class NotificationChannel(StrEnum):
    """How delivery was attempted.

    Mirrors `components.schemas.NotificationChannel` and the
    `notification_channel` Postgres enum. `PUSH` means the user had a device
    registered when it was sent; `IN_APP` means they had none.
    """

    PUSH = "PUSH"
    IN_APP = "IN_APP"
    EMAIL = "EMAIL"


# ── Responses ───────────────────────────────────────────────────────────


class NotificationOut(BaseModel):
    """One entry in the caller's notification centre.

    Mirrors `components.schemas.Notification`.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(description="Unique identifier")
    type: NotificationType = Field(description="Why this notification was sent")
    channel: NotificationChannel = Field(description="How delivery was attempted")
    title: str = Field(description="Short headline", examples=["Update on ISS-2026-K7QD3M8XPZ"])
    body: str = Field(
        description="Full notification text",
        examples=["Your pothole report on MG Road is being addressed."],
    )
    issue_id: uuid.UUID | None = Field(
        default=None,
        description="Issue this notification is about, for navigation; null if none or if the issue was deleted",
    )
    is_read: bool = Field(description="Whether the caller has marked it read")
    created_at: datetime = Field(description="ISO 8601 timestamp the notification was created")


class NotificationListResponse(PaginatedResponse[NotificationOut]):
    """One page of the caller's notifications, newest first, plus the badge count.

    Mirrors `components.schemas.NotificationListResponse`. `unread_count`
    counts across *all* of the caller's notifications, not just this page and
    not just the `unread_only` filter, so the bell badge is correct from any
    page.
    """

    unread_count: int = Field(ge=0, description="Total unread notifications for the caller")


class MarkAllNotificationsReadResponse(BaseModel):
    """Result of `PATCH /notifications/read-all`.

    Mirrors `components.schemas.MarkAllNotificationsReadResponse`.
    """

    updated_count: int = Field(ge=0, description="How many notifications changed from unread to read")
