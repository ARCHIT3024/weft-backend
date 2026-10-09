"""Domain events for the realtime dashboard — the transport half.

What lives here is deliberately small: the event names, the JSON envelope,
the Redis channel naming, and a `publish_event` that **never raises**. What an
event *means* (which issue fields go in it, when it fires) is decided in
`app/services/realtime_service.py`; who receives it is decided by
`app/routers/websocket.py`. The protocol as a whole is documented for client
authors in `docs/realtime.md`.

Two rules every caller relies on:

* **Publish only after commit.** Nothing here knows about transactions, so
  callers schedule publishing as a FastAPI background task, which runs after
  the request session has committed — the same mechanism as
  `notification_service.schedule_status_change_notification`. An event for a
  change that rolled back would be a lie told to every connected dashboard.
* **Fail open.** A Redis outage must never fail an issue submission, a status
  change or an upvote (same philosophy as D-8). The realtime feed is a
  convenience over data that the REST API already serves; losing an event
  costs a dashboard a refresh, losing a submission costs a citizen a report.

Redis pub/sub is fire-and-forget: a subscriber that is not connected at the
moment of publishing never sees the event. That is acceptable precisely because
of the point above — the dashboard re-reads the REST API whenever it
(re)connects — and it is why there is no event store here.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis

logger = logging.getLogger(__name__)

# TRD §5 "Redis Pub/Sub Channel Convention": one channel per zone. Read at call
# time rather than baked into constants derived from it, so a test can move the
# whole feed onto a private prefix and cannot cross-talk with a dev server or
# another test run sharing the same Redis — pub/sub ignores the DB number.
CHANNEL_PREFIX = "ws:zone:"

# An issue whose location falls outside every zone still has to reach someone.
# It is published here, and only admins (who pattern-subscribe to every zone
# channel, which this matches too) receive it. "none" cannot collide with a
# zone channel, because zone ids are UUIDs.
NO_ZONE_CHANNEL_SUFFIX = "none"

# `issue.high_upvote_alert` deduplication marker, one key per issue. See
# `app/services/realtime_service.py::publish_high_upvote_alert` for why it
# exists. Bounded rather than permanent so the keyspace cannot grow without
# limit; six months comfortably outlives any issue still collecting votes.
ALERT_MARKER_PREFIX = "events:high_upvote_alert:"
ALERT_MARKER_TTL_SECONDS = 180 * 24 * 60 * 60


class EventType(StrEnum):
    """Every event a dashboard can receive. The catalogue is `docs/realtime.md`."""

    ISSUE_CREATED = "issue.created"
    ISSUE_STATUS_CHANGED = "issue.status_changed"
    ISSUE_ASSIGNED = "issue.assigned"
    ISSUE_HIGH_UPVOTE_ALERT = "issue.high_upvote_alert"


class DomainEvent(BaseModel):
    """The JSON envelope every event travels in, on Redis and on the socket.

    The routing fields (`zone_id`, `department_id`) sit at the top level so a
    client can filter or route without reaching into `data`. `data.issue` is
    always an `IssueSummary`, field for field, so the dashboard can add or move
    a map marker without a refetch. `event_id` lets a client drop a duplicate
    if it ever holds two sockets across a reconnect.

    Serialised by Pydantic so timestamps here match the REST API's format
    exactly (UTC, `Z` suffix).
    """

    model_config = ConfigDict(frozen=True)

    type: EventType
    event_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    issue_id: uuid.UUID
    zone_id: uuid.UUID | None
    department_id: uuid.UUID | None
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    data: dict[str, Any]


def zone_channel(zone_id: uuid.UUID | None) -> str:
    """The Redis channel for a zone, or the admin-only channel for no zone."""
    return f"{CHANNEL_PREFIX}{zone_id if zone_id is not None else NO_ZONE_CHANNEL_SUFFIX}"


def all_zones_pattern() -> str:
    """A `PSUBSCRIBE` pattern matching every zone channel, the no-zone one included."""
    return f"{CHANNEL_PREFIX}*"


def alert_marker_key(issue_id: uuid.UUID) -> str:
    """The Redis key recording that an issue's high-upvote alert has fired."""
    return f"{ALERT_MARKER_PREFIX}{issue_id}"


async def publish_event(redis: Redis | None, event: DomainEvent) -> bool:
    """Publish one event to its zone's channel. **Never raises.**

    Returns whether Redis accepted it (not whether anyone was listening). Meant
    to run as a background task: there is no caller to hand an exception to,
    and the change being announced has already been committed.
    """
    if redis is None:
        logger.warning("Realtime event %s for issue_id=%s not published: no Redis client.", event.type, event.issue_id)
        return False
    channel = zone_channel(event.zone_id)
    try:
        await redis.publish(channel, event.model_dump_json())
    except Exception:
        # Fail open (D-8). The bounded socket timeouts on the shared pool keep
        # this from hanging, and the dashboard recovers on its next refetch.
        logger.warning(
            "Realtime event %s for issue_id=%s could not be published to %s.",
            event.type,
            event.issue_id,
            channel,
            exc_info=True,
        )
        return False
    return True
