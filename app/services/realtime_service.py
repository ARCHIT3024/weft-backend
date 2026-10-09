"""Realtime dashboard feed — what each event says, when it fires, who hears it.

Two halves, mirroring `notification_service`:

* **Publishing** (`schedule_*`) is called by route handlers once the service
  call has succeeded. It snapshots the issue into plain JSON *at request time*
  — the ORM row belongs to a session that is closed by the time the task runs —
  and queues the publish as a FastAPI background task. FastAPI commits the
  request session before background tasks run, and does not run them at all
  if the request failed, so an event is only ever published for a change that
  committed. Publishing never raises; see `app/core/events.py`.
* **Subscribing** (`authenticate`, `load_subscriber`) turns an access token
  into the set of Redis channels a dashboard socket may listen on. It is used
  by `app/routers/websocket.py` at connect time and again periodically, so a
  deactivated account or a changed zone assignment does not keep a socket
  open on stale authority.

The wire protocol is documented in `docs/realtime.md`.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastapi import BackgroundTasks
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from app.core import events
from app.core.events import DomainEvent, EventType
from app.core.exceptions import ForbiddenError, UnauthorizedError
from app.core.permissions import Role
from app.core.security import decode_jwt
from app.models.authority_user import AuthorityUser
from app.models.authority_zone import AuthorityZone
from app.models.department import Department
from app.models.user import User
from app.schemas.issue import IssueSummary

if TYPE_CHECKING:
    from app.models.issue import Issue

logger = logging.getLogger(__name__)

SessionBind = AsyncEngine | AsyncConnection

# Roles allowed on the dashboard socket. Citizens are refused: the feed carries
# every issue in a zone, including other people's reports.
STAFF_ROLES = frozenset({Role.AUTHORITY.value, Role.ADMIN.value})

_SUMMARY_FIELDS = frozenset(IssueSummary.model_fields)


# ── Publishing ──────────────────────────────────────────────────────────


def _issue_payload(issue: IssueSummary) -> dict[str, Any]:
    """An `IssueSummary` as JSON-ready data, field for field.

    Accepts any subclass (an `IssueDetail` is what the triage routes already
    hold) but emits only the summary's fields: no photos, no audit trail. An
    event should be cheap to fan out to every dashboard in a zone.
    """
    return issue.model_dump(mode="json", include=set(_SUMMARY_FIELDS))


def _event(event_type: EventType, issue: IssueSummary, **extra: Any) -> DomainEvent:
    """Wrap an issue in the envelope. `extra` values may be UUIDs or datetimes:
    Pydantic serialises them in the same format as the REST API."""
    return DomainEvent(
        type=event_type,
        issue_id=issue.id,
        zone_id=issue.zone_id,
        department_id=issue.department_id,
        data={"issue": _issue_payload(issue), **extra},
    )


def schedule_issue_created(background_tasks: BackgroundTasks, redis: Redis | None, *, issue: IssueSummary) -> None:
    """Queue `issue.created` for after the submission commits."""
    background_tasks.add_task(events.publish_event, redis, _event(EventType.ISSUE_CREATED, issue))


def schedule_status_changed(
    background_tasks: BackgroundTasks,
    redis: Redis | None,
    *,
    issue: IssueSummary,
    previous_status: str | None,
    actor: User,
    note: str | None,
) -> None:
    """Queue `issue.status_changed` for after the transition commits."""
    event = _event(
        EventType.ISSUE_STATUS_CHANGED,
        issue,
        previous_status=previous_status,
        new_status=str(issue.status),
        changed_by_id=actor.id,
        note=note,
    )
    background_tasks.add_task(events.publish_event, redis, event)


def schedule_assigned(
    background_tasks: BackgroundTasks,
    redis: Redis | None,
    *,
    issue: IssueSummary,
    assigned_at: datetime | None,
    actor: User,
) -> None:
    """Queue `issue.assigned` for after the assignment commits."""
    event = _event(
        EventType.ISSUE_ASSIGNED,
        issue,
        assigned_to_id=issue.assigned_to_id,
        assigned_at=assigned_at,
        assigned_by_id=actor.id,
    )
    background_tasks.add_task(events.publish_event, redis, event)


async def schedule_high_upvote_alert(
    background_tasks: BackgroundTasks,
    db: AsyncSession,
    redis: Redis | None,
    *,
    issue: Issue,
) -> bool:
    """Queue `issue.high_upvote_alert` if this upvote just reached the threshold.

    Call after an upvote has been inserted and the issue re-read, so
    `issue.upvote_count` is the `trg_upvote_count` trigger's post-insert value.
    Returns whether an alert was queued (not whether it will be published —
    the deduplication marker below has the last word).

    "Fires once" is guaranteed in two layers:

    1. **Only on the upvote that makes the count *equal* the threshold**, not
       on every upvote at or above it. The trigger serialises concurrent votes
       on the issue row, so exactly one committed upvote observes the count
       landing on the threshold.
    2. **A Redis `SET NX` marker per issue**, taken in the background task
       just before publishing. Without it, withdrawing and re-adding a vote at
       the boundary (threshold → threshold-1 → threshold) would fire again on
       every round trip — an alert a single user could spam. With it, only the
       first crossing ever publishes.

    Consequences, accepted deliberately: an issue with no department has no
    threshold and never alerts; an issue already past a threshold that an
    admin later *lowers* does not alert retroactively; and if Redis is down at
    the crossing moment the alert is lost (fail open — the issue's
    `upvote_count` still shows it on the dashboard).
    """
    if issue.department_id is None:
        return False
    threshold = await db.scalar(select(Department.upvote_alert_threshold).where(Department.id == issue.department_id))
    if threshold is None or issue.upvote_count != threshold:
        return False

    summary = IssueSummary.model_validate(issue)
    event = _event(EventType.ISSUE_HIGH_UPVOTE_ALERT, summary, upvote_count=issue.upvote_count, threshold=threshold)
    background_tasks.add_task(publish_high_upvote_alert, redis, event)
    return True


async def publish_high_upvote_alert(redis: Redis | None, event: DomainEvent) -> bool:
    """Background task: take the per-issue marker, and publish only if it was free.

    **Never raises.** If the marker cannot be taken because Redis is down, the
    publish would fail too, so the alert is simply dropped and logged.
    """
    if redis is None:
        logger.warning("High-upvote alert for issue_id=%s not published: no Redis client.", event.issue_id)
        return False
    try:
        first = await redis.set(
            events.alert_marker_key(event.issue_id),
            event.event_id.hex,
            nx=True,
            ex=events.ALERT_MARKER_TTL_SECONDS,
        )
    except Exception:
        logger.warning("High-upvote alert for issue_id=%s dropped: Redis unavailable.", event.issue_id, exc_info=True)
        return False
    if not first:
        logger.info("High-upvote alert for issue_id=%s already fired; not repeating it.", event.issue_id)
        return False
    return await events.publish_event(redis, event)


# ── Subscribing ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Subscriber:
    """Who is on a dashboard socket, and what they may hear.

    `expires_at` is the access token's `exp`: the socket is closed then, so a
    connection can never outlive the credential that opened it.
    """

    user_id: uuid.UUID
    role: str
    zone_ids: frozenset[uuid.UUID]
    all_zones: bool
    expires_at: datetime

    @property
    def channels(self) -> list[str]:
        """Exact channels to `SUBSCRIBE` to. Empty for an admin, who uses a pattern."""
        if self.all_zones:
            return []
        return sorted(events.zone_channel(zone_id) for zone_id in self.zone_ids)

    @property
    def patterns(self) -> list[str]:
        """Patterns to `PSUBSCRIBE` to: every zone plus the no-zone channel, admins only."""
        return [events.all_zones_pattern()] if self.all_zones else []

    def same_scope_as(self, other: Subscriber) -> bool:
        """Whether `other` would be subscribed to exactly the same channels."""
        return (self.role, self.all_zones, self.zone_ids) == (other.role, other.all_zones, other.zone_ids)


def _session_for(bind: SessionBind) -> AsyncSession:
    """A short-lived session on the socket's bind.

    Same reasoning as `notification_service._session_for`: a WebSocket lives
    for hours, so it must not pin a pooled connection for its lifetime — each
    lookup opens and closes its own session. Under test the bind is the test's
    connection, and `create_savepoint` keeps this session from touching the
    test's outer transaction.
    """
    return AsyncSession(bind=bind, expire_on_commit=False, join_transaction_mode="create_savepoint")


async def authenticate(bind: SessionBind, token: str) -> Subscriber:
    """Validate an access token for the dashboard socket.

    Raises:
        UnauthorizedError: bad signature, expired, no `exp`, malformed subject,
            or the account is missing or deactivated — the same `is_active`
            re-check `get_current_user` makes on every REST request.
        ForbiddenError: the account is valid but is not AUTHORITY or ADMIN.
    """
    payload = decode_jwt(token)
    exp = payload.get("exp")
    if not isinstance(exp, int | float):
        # A token without an expiry would let a socket stay open forever.
        raise UnauthorizedError("Invalid token: missing expiry claim")
    try:
        user_id = uuid.UUID(str(payload["sub"]))
    except ValueError as exc:
        raise UnauthorizedError("Invalid token: malformed subject claim") from exc
    return await load_subscriber(bind, user_id=user_id, expires_at=datetime.fromtimestamp(exp, UTC))


async def load_subscriber(bind: SessionBind, *, user_id: uuid.UUID, expires_at: datetime) -> Subscriber:
    """Read the account's current standing and zones from the database.

    The role is taken from `users.role`, not from the token, so a demotion
    takes effect at the next check rather than when the token expires.

    Raises:
        UnauthorizedError: the account is missing or deactivated.
        ForbiddenError: the account is not staff.
    """
    async with _session_for(bind) as session:
        user = await session.get(User, user_id)
        if user is None or not user.is_active:
            raise UnauthorizedError("Account deactivated or not found")
        role = str(user.role)
        if role not in STAFF_ROLES:
            raise ForbiddenError("The dashboard feed is for authority staff only")

        if role == Role.ADMIN.value:
            return Subscriber(user_id=user_id, role=role, zone_ids=frozenset(), all_zones=True, expires_at=expires_at)

        # An authority without an `authority_users` profile, or with no zones,
        # is let in with nothing to hear rather than refused: the dashboard can
        # then say "no zones assigned" instead of failing to connect.
        zone_ids = (
            await session.scalars(
                select(AuthorityZone.zone_id)
                .join(AuthorityUser, AuthorityUser.id == AuthorityZone.authority_user_id)
                .where(AuthorityUser.user_id == user_id)
            )
        ).all()
        return Subscriber(
            user_id=user_id, role=role, zone_ids=frozenset(zone_ids), all_zones=False, expires_at=expires_at
        )
