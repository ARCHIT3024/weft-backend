"""User self-service — profile edits and push-device registration.

Everything here acts on the *caller's own* account. Nothing takes a user id
from the request: the router passes the authenticated `User`, so there is no
parameter through which one account could edit another.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.push import redact_token
from app.models.fcm_token import FcmToken
from app.models.user import User

logger = logging.getLogger(__name__)

# Devices kept per account; registering one more evicts the least recently
# seen. Generous for a person with a phone and a tablet, and a hard ceiling on
# what a scripted client can make every status change fan out to.
MAX_DEVICE_TOKENS_PER_USER = 10


async def update_profile(
    db: AsyncSession,
    *,
    user: User,
    name: str | None = None,
    preferred_lang: str | None = None,
    fcm_token: str | None = None,
) -> User:
    """Apply a partial profile update. `None` means "leave unchanged"."""
    if name is not None:
        user.name = name
    if preferred_lang is not None:
        user.preferred_lang = preferred_lang
    if fcm_token is not None:
        await register_device_token(db, user_id=user.id, token=fcm_token)

    await db.flush()
    # `updated_at` is set by the database on UPDATE, which expires it on the
    # instance; re-read so no attribute is left to lazy-load outside a greenlet.
    await db.refresh(user)
    return user


async def register_device_token(db: AsyncSession, *, user_id: uuid.UUID, token: str) -> None:
    """Record that `token` now belongs to `user_id`, refreshing `last_seen`.

    One atomic upsert on `UNIQUE (device_token)`, not a SELECT-then-write: if
    two accounts register the same token at once — a phone changing hands —
    the database settles who ends up owning it, and it never belongs to both.
    Re-registering a token another user held **moves** it; that previous
    user's notifications stop arriving on a device someone else now holds.

    `clock_timestamp()` rather than `NOW()` so that several registrations in
    one transaction still order correctly for the eviction below.
    """
    now = func.clock_timestamp()
    stmt = (
        pg_insert(FcmToken)
        .values(id=uuid.uuid4(), user_id=user_id, device_token=token, last_seen=now)
        .on_conflict_do_update(
            index_elements=[FcmToken.device_token],
            set_={"user_id": user_id, "last_seen": now},
        )
    )
    await db.execute(stmt)

    # Keep only the most recently seen devices. Tokens the app stopped
    # re-registering (uninstalled, rotated) age out here even if FCM never got
    # the chance to report them dead.
    keep = (
        select(FcmToken.id)
        .where(FcmToken.user_id == user_id)
        .order_by(FcmToken.last_seen.desc(), FcmToken.id.desc())
        .limit(MAX_DEVICE_TOKENS_PER_USER)
    )
    await db.execute(delete(FcmToken).where(FcmToken.user_id == user_id, FcmToken.id.not_in(keep)))

    logger.info("Device token registered user_id=%s token=%s", user_id, redact_token(token))
