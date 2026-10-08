"""WebSocket router — mock stubs for Phase 0.

The real implementation (Phase 1, task 1.27) will:
- Validate JWT from query string on connect
- Subscribe to Redis pub/sub channels per authority zone
- Fanout issue events to connected dashboard clients
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

router = APIRouter()


@router.websocket("/dashboard")
async def websocket_dashboard_mock(websocket: WebSocket) -> None:
    """Mock: WebSocket endpoint for authority dashboard real-time updates.

    In Phase 0, this just accepts the connection and sends a welcome message.
    The real implementation will:
    1. Validate JWT from ?token= query param (reject with 4001 if invalid)
    2. Look up authority's zone assignments
    3. Subscribe to Redis channels ws:zone:{zone_id}
    4. Fanout events (issue.created, issue.status_changed, etc.)
    """
    await websocket.accept()
    logger.info("WebSocket mock connection accepted")

    # Send a welcome event to confirm connection
    await websocket.send_json(
        {
            "event": "connection.established",
            "timestamp": "2026-07-22T12:00:00Z",
            "data": {
                "message": "Connected to Weft dashboard WebSocket (mock mode)",
            },
        }
    )

    try:
        while True:
            # Keep connection alive; echo back any received messages
            data = await websocket.receive_text()
            logger.debug("WebSocket mock received: %s", data)
            await websocket.send_json(
                {
                    "event": "echo",
                    "data": {"received": data},
                }
            )
    except WebSocketDisconnect:
        logger.info("WebSocket mock client disconnected")
