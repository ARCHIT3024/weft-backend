"""Notifications router — mock stubs for Phase 0."""

from __future__ import annotations

import logging

from fastapi import APIRouter, status

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("")
async def list_notifications_mock() -> dict:
    """Mock: Get notification list for current user."""
    return {
        "items": [
            {
                "id": "notif-uuid-001",
                "type": "STATUS_CHANGE",
                "channel": "PUSH",
                "title": "Issue Updated",
                "body": "Your reported pothole on Anna Salai is now In Progress.",
                "issue_id": "550e8400-e29b-41d4-a716-446655440000",
                "is_read": False,
                "created_at": "2026-07-02T10:30:00Z",
            }
        ],
        "total": 1,
        "page": 1,
        "page_size": 20,
        "total_pages": 1,
    }


@router.patch("/{notification_id}/read")
async def mark_read_mock(notification_id: str) -> dict:
    """Mock: Mark notification as read."""
    return {"id": notification_id, "is_read": True}


@router.patch("/read-all", status_code=status.HTTP_200_OK)
async def mark_all_read_mock() -> dict:
    """Mock: Mark all notifications as read."""
    return {"updated_count": 1}
