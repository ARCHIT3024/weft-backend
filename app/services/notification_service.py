"""Notifications — the in-app centre, and push delivery for it.

Two halves with different failure rules:

* **Reading and marking** (`list_notifications`, `mark_read`,
  `mark_all_read`) run in the request's session like any other query, and are
  always scoped to the caller. Another user's notification is a 404, never a
  403 — a 403 would confirm that the id exists, turning the endpoint into an
  oracle for probing ids.
* **Delivery** (`deliver`, `notify_status_change`) runs *after* the response,
  as a FastAPI background task, and **never raises**. It is a side effect of
  something that has already succeeded — an authority's status change has been
  committed by the time it runs — and a push failure must not be able to turn
  that success into an error, or into an unhandled exception in the server
  log with nobody to report it to.

Delivery opens its own session(s), because the request session is closed by
the time a background task runs. It is opened on the request session's *bind*
rather than on `app.database.async_session_factory` — see `_session_for`.

Flow for one notification (TRD §8):

1. Write the `notifications` row and read the user's device tokens; commit.
   The row exists from here on regardless of what push does — the in-app
   centre is the source of truth.
2. Send to each token through `app.core.push` (no session held — a pooled
   connection is not parked across up to three HTTP attempts with backoff).
3. Record the outcome: `sent_at` if any device accepted it, `retry_count` as
   the failed attempts, and delete every token FCM called dead.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import BackgroundTasks
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from app.core.exceptions import NotFoundError
from app.core.push import PushMessage, PushOutcome, PushResult, get_push_sender, redact_token
from app.models.fcm_token import FcmToken
from app.models.notification import Notification
from app.models.user import User

if TYPE_CHECKING:
    from app.models.issue import Issue

logger = logging.getLogger(__name__)

# `notifications.title` is VARCHAR(255).
TITLE_MAX_LENGTH = 255
# An address is citizen-supplied free text of up to 500 characters. A push
# body has to fit on a lock screen, so it is shortened there.
_ADDRESS_MAX_IN_BODY = 80
# `retry_count` is SMALLINT.
_RETRY_COUNT_CEILING = 32_767

SessionBind = AsyncEngine | AsyncConnection


# ── Reading and marking (request-scoped) ────────────────────────────────


async def list_notifications(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    page: int = 1,
    page_size: int = 20,
    unread_only: bool = False,
) -> tuple[list[Notification], int, int]:
    """One page of a user's notifications, newest first.

    Returns `(rows, total, unread_count)`. `total` counts what the filter
    matches; `unread_count` always counts every unread row, so the bell badge
    is right whichever page or filter the client is on.
    """
    base = select(Notification).where(Notification.user_id == user_id)
    if unread_only:
        base = base.where(Notification.is_read.is_(False))

    total = await db.scalar(select(func.count()).select_from(base.subquery())) or 0
    unread_count = await count_unread(db, user_id=user_id)

    stmt = (
        base.order_by(Notification.created_at.desc(), Notification.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = (await db.scalars(stmt)).all()
    return list(rows), total, unread_count


async def count_unread(db: AsyncSession, *, user_id: uuid.UUID) -> int:
    """How many of this user's notifications are unread."""
    stmt = (
        select(func.count())
        .select_from(Notification)
        .where(Notification.user_id == user_id, Notification.is_read.is_(False))
    )
    return await db.scalar(stmt) or 0


async def mark_read(db: AsyncSession, *, user_id: uuid.UUID, notification_id: uuid.UUID) -> Notification:
    """Mark one of the caller's notifications read. Idempotent.

    Ownership is part of the lookup, not a check after it: a notification that
    belongs to someone else is indistinguishable from one that does not exist,
    and both are a 404.
    """
    stmt = select(Notification).where(Notification.id == notification_id, Notification.user_id == user_id)
    notification = await db.scalar(stmt)
    if notification is None:
        raise NotFoundError("Notification")

    if not notification.is_read:
        notification.is_read = True
        await db.flush()
    return notification


async def mark_all_read(db: AsyncSession, *, user_id: uuid.UUID) -> int:
    """Mark every unread notification of the caller read. Returns how many changed."""
    result = await db.execute(
        update(Notification)
        .where(Notification.user_id == user_id, Notification.is_read.is_(False))
        .values(is_read=True)
        .execution_options(synchronize_session=False)
    )
    await db.flush()
    return result.rowcount or 0


# ── Delivery (background) ───────────────────────────────────────────────


@dataclass(frozen=True)
class StatusChangeNotice:
    """Everything a status-change notification needs, captured at request time.

    Plain values, not the ORM `Issue`: the background task runs after the
    request session has closed, and an ORM instance from a closed session is a
    detached object waiting to raise on its first lazy attribute.
    """

    reporter_id: uuid.UUID
    issue_id: uuid.UUID
    issue_number: str
    category: str
    address_text: str | None
    new_status: str


def _session_for(bind: SessionBind) -> AsyncSession:
    """A fresh session on the same bind as the request that scheduled the work.

    In production the request session is bound to the engine, so this is just a
    new pooled session — the request's own session is already closed.

    Under test the request session is bound to one connection holding an outer
    transaction that is rolled back at the end of the test. Binding here means
    the background work sees the test's uncommitted rows (the issue, its
    reporter) and is rolled back with everything else, instead of writing to
    whatever `DATABASE_URL` points at. `create_savepoint` makes this session's
    commit/rollback act on a SAVEPOINT, so a failure in delivery cannot roll
    back the test's outer transaction either. Against an engine the mode has no
    effect.
    """
    return AsyncSession(bind=bind, expire_on_commit=False, join_transaction_mode="create_savepoint")


def _human_category(category: str) -> str:
    """`GARBAGE_ACCUMULATION` → `garbage accumulation`."""
    return category.replace("_", " ").lower()


def build_status_change_message(notice: StatusChangeNotice) -> tuple[str, str]:
    """`(title, body)` for a status change, per TRD §8 "Notification Trigger Table".

    English only for now; `users.preferred_lang` is stored but there are no
    translated templates yet (task 4.13 territory).
    """
    title = f"Update on {notice.issue_number}"[:TITLE_MAX_LENGTH]

    subject = f"Your {_human_category(notice.category)} report"
    if notice.address_text:
        address = notice.address_text.strip()
        if len(address) > _ADDRESS_MAX_IN_BODY:
            address = address[: _ADDRESS_MAX_IN_BODY - 1].rstrip() + "…"
        subject = f"{subject} on {address}"

    if notice.new_status == "IN_PROGRESS":
        body = f"{subject} is being addressed."
    elif notice.new_status == "RESOLVED":
        body = f"{subject} has been resolved."
    elif notice.new_status == "REJECTED":
        body = "Your report was reviewed and could not be actioned."
    else:
        body = f"{subject} is now {notice.new_status.replace('_', ' ').lower()}."
    return title, body


def schedule_status_change_notification(
    background_tasks: BackgroundTasks,
    db: AsyncSession,
    *,
    issue: Issue,
    actor: User,
) -> None:
    """Queue the reporter's notification for after the response is sent.

    Called by the status-change route once `issue_service.update_status` has
    succeeded. Skips anonymous issues (no reporter to tell) and actors who
    changed the status of their own report (nobody needs to be told what they
    just did). By the time the task runs, FastAPI has already committed the
    request session, so it never notifies about a change that rolled back.
    """
    if issue.reporter_id is None or issue.reporter_id == actor.id:
        return
    notice = StatusChangeNotice(
        reporter_id=issue.reporter_id,
        issue_id=issue.id,
        issue_number=issue.issue_number,
        category=str(issue.category),
        address_text=issue.address_text,
        new_status=str(issue.status),
    )
    background_tasks.add_task(notify_status_change, db.bind, notice)


async def notify_status_change(bind: SessionBind, notice: StatusChangeNotice) -> None:
    """Background task: tell the reporter their issue moved. Never raises."""
    title, body = build_status_change_message(notice)
    await deliver(
        bind,
        user_id=notice.reporter_id,
        notification_type="STATUS_CHANGE",
        title=title,
        body=body,
        issue_id=notice.issue_id,
        data={
            "issue_id": str(notice.issue_id),
            "issue_number": notice.issue_number,
            "new_status": notice.new_status,
            "deep_link": f"weft://issues/{notice.issue_id}",
        },
    )


async def deliver(
    bind: SessionBind,
    *,
    user_id: uuid.UUID,
    notification_type: str,
    title: str,
    body: str,
    issue_id: uuid.UUID | None = None,
    data: dict[str, str] | None = None,
) -> uuid.UUID | None:
    """Record a notification and push it to the user's devices. Never raises.

    Returns the notification id, or None if nothing could be recorded (the user
    is gone or deactivated, or the database write failed). Every failure is
    logged with its traceback; none propagates. A background task has no caller
    to hand an exception to, and the action that triggered it has already
    succeeded.
    """
    try:
        recorded = await _record(
            bind,
            user_id=user_id,
            notification_type=notification_type,
            title=title[:TITLE_MAX_LENGTH],
            body=body,
            issue_id=issue_id,
        )
    except Exception:
        logger.exception("Could not record %s notification for user_id=%s", notification_type, user_id)
        return None
    if recorded is None:
        return None

    notification_id, tokens = recorded
    if not tokens:
        return notification_id

    try:
        results = await _push(
            tokens, PushMessage(title=title, body=body, data={**(data or {}), "notification_id": str(notification_id)})
        )
    except Exception:
        # Building the sender failed — FCM configured but unusable. The row is
        # already written; leave `sent_at` NULL so the failure stays visible.
        logger.exception("Push delivery skipped for notification_id=%s: push sender unavailable", notification_id)
        return notification_id

    try:
        await _record_outcome(bind, notification_id=notification_id, user_id=user_id, results=results)
    except Exception:
        logger.exception("Could not record push outcome for notification_id=%s", notification_id)
    return notification_id


async def _record(
    bind: SessionBind,
    *,
    user_id: uuid.UUID,
    notification_type: str,
    title: str,
    body: str,
    issue_id: uuid.UUID | None,
) -> tuple[uuid.UUID, list[str]] | None:
    """Step 1: write the row and read the device tokens, in one short transaction."""
    async with _session_for(bind) as session:
        user = await session.get(User, user_id)
        if user is None or not user.is_active:
            # A deactivated account is not told anything, and its devices are
            # not pushed to: the person may no longer be the one holding them.
            logger.info("Notification skipped: user_id=%s is missing or deactivated", user_id)
            return None

        tokens = list(
            (
                await session.scalars(
                    select(FcmToken.device_token).where(FcmToken.user_id == user_id).order_by(FcmToken.last_seen.desc())
                )
            ).all()
        )
        notification = Notification(
            user_id=user_id,
            issue_id=issue_id,
            type=notification_type,
            channel="PUSH" if tokens else "IN_APP",
            title=title,
            body=body,
        )
        session.add(notification)
        await session.commit()
        return notification.id, tokens


async def _push(tokens: Sequence[str], message: PushMessage) -> dict[str, PushResult]:
    """Step 2: send to every device. No database session is held here."""
    sender = get_push_sender()
    results: dict[str, PushResult] = {}
    for token in tokens:
        try:
            results[token] = await sender.send(token, message)
        except Exception as exc:
            # `PushSender.send` promises not to raise; a sender that does
            # still must not cost the other devices their notification.
            logger.exception("Push sender raised for token=%s", redact_token(token))
            results[token] = PushResult(outcome=PushOutcome.FAILED, attempts=1, error=str(exc))
    return results


async def _record_outcome(
    bind: SessionBind,
    *,
    notification_id: uuid.UUID,
    user_id: uuid.UUID,
    results: dict[str, PushResult],
) -> None:
    """Step 3: `sent_at`, `retry_count`, and dead-token cleanup."""
    delivered = any(r.outcome is PushOutcome.DELIVERED for r in results.values())
    failed_attempts = min(sum(r.failed_attempts for r in results.values()), _RETRY_COUNT_CEILING)
    dead = [token for token, r in results.items() if r.outcome is PushOutcome.INVALID_TOKEN]

    async with _session_for(bind) as session:
        await session.execute(
            update(Notification)
            .where(Notification.id == notification_id)
            .values(
                sent_at=datetime.now(UTC) if delivered else None,
                retry_count=failed_attempts,
            )
            .execution_options(synchronize_session=False)
        )
        if dead:
            # Scoped to this user: a token can change owner between the read in
            # step 1 and now, and a fresh registration by someone else must not
            # be deleted on the strength of an answer about the old one.
            await session.execute(
                delete(FcmToken)
                .where(FcmToken.user_id == user_id, FcmToken.device_token.in_(dead))
                .execution_options(synchronize_session=False)
            )
            logger.info(
                "Removed %d dead device token(s) for user_id=%s: %s",
                len(dead),
                user_id,
                ", ".join(redact_token(t) for t in dead),
            )
        await session.commit()

    if not delivered:
        logger.warning(
            "Notification %s was not pushed to any device (%d failed attempt(s)); it remains in the in-app centre",
            notification_id,
            failed_attempts,
        )
