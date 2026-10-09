"""WebSocket router — the authority dashboard's realtime feed (task 1.27).

`WS /v1/ws/dashboard`. The full protocol, every close code and every event
shape is documented for client authors in `docs/realtime.md`; this docstring
covers why it is built the way it is.

**The token travels in the first message, not the URL.** A browser cannot set
an `Authorization` header on a WebSocket, and the TRD's `?token=` would write a
24-hour bearer credential into every proxy and access log between the browser
and the API. The alternative, smuggling it through `Sec-WebSocket-Protocol`,
puts it in a header that proxies also log and that the server is expected to
echo back. So the socket is accepted unauthenticated and the client has
`AUTH_TIMEOUT_SECONDS` to send `{"type": "auth", "token": "..."}`; until then
nothing is subscribed and nothing is sent. Because authentication is an
explicit token rather than a cookie, cross-site WebSocket hijacking has nothing
to ride on.

**Fan-out is Redis, not process memory.** Each socket holds its own pub/sub
subscription, so an event published by any API instance reaches every
dashboard on every instance — which is the point of the design.

**A socket never outlives its authority.** It is closed at the token's `exp`
(the client refreshes and reconnects), and the account is re-read from the
database every `REVALIDATE_INTERVAL_SECONDS`: deactivation, demotion or a
changed zone assignment all close the socket rather than leaving it streaming
on stale permissions until the token runs out.

**Nothing leaks on disconnect.** Each connection runs two tasks — one pumping
Redis to the client, one reading the client — and whichever ends first cancels
the other; the pub/sub connection is closed in a `finally`, so a dropped
client, a server shutdown or a Redis failure all release it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import UTC, datetime
from enum import IntEnum
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from redis.asyncio import Redis
from redis.asyncio.client import PubSub

from app.core.exceptions import ForbiddenError, UnauthorizedError
from app.dependencies import DBSession
from app.services import realtime_service
from app.services.realtime_service import SessionBind, Subscriber

logger = logging.getLogger(__name__)

router = APIRouter()

# Module attributes, read at use time, so tests can shorten them.
#
# Long enough for a client on a slow link to send one small frame after the
# handshake; short enough that an idle unauthenticated socket is not a cheap
# way to hold server resources.
AUTH_TIMEOUT_SECONDS = 5.0
# Under the 60s idle timeout of a default AWS ALB and of most reverse proxies,
# with margin. Uvicorn's protocol-level pings keep the TCP path alive too, but
# a browser cannot see those; an application-level ping lets the dashboard
# notice a dead server instead of waiting on a silent socket.
HEARTBEAT_INTERVAL_SECONDS = 25.0
# How stale a socket's permissions may get. One primary-key read and one small
# join per connection per minute.
REVALIDATE_INTERVAL_SECONDS = 60.0
# Upper bound on one wait for a pub/sub message, and so on how late the expiry
# and heartbeat checks can run. Passed to redis-py as an explicit read timeout,
# which takes precedence over the shared pool's 1s socket timeout.
POLL_INTERVAL_SECONDS = 1.0
# How long Redis may take to confirm a new socket's subscriptions.
SUBSCRIBE_TIMEOUT_SECONDS = 5.0


class CloseCode(IntEnum):
    """Close codes the server uses. The client's reaction to each is in `docs/realtime.md`."""

    # RFC 6455 standard codes.
    INTERNAL_ERROR = 1011
    TRY_AGAIN_LATER = 1013
    # Application codes (4000-4999), numbered after the HTTP status they echo.
    PROTOCOL_ERROR = 4400
    UNAUTHORIZED = 4401
    FORBIDDEN = 4403
    AUTH_TIMEOUT = 4408
    SUBSCRIPTION_CHANGED = 4409


class _CloseSocket(Exception):  # noqa: N818 — a control-flow signal, not an error report
    """Raised inside the connection to end it with a specific close code."""

    def __init__(self, code: CloseCode, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


class _Sender:
    """Serialises writes: the pump and the reader both send, from separate tasks."""

    def __init__(self, websocket: WebSocket) -> None:
        self._websocket = websocket
        self._lock = asyncio.Lock()

    async def text(self, data: str) -> None:
        async with self._lock:
            await self._websocket.send_text(data)

    async def json(self, payload: dict[str, Any]) -> None:
        await self.text(json.dumps(payload, separators=(",", ":")))


def _utc_iso(moment: datetime) -> str:
    """Same timestamp format as the REST API and the event envelope: UTC with `Z`."""
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_client_message(message: dict[str, Any]) -> dict[str, Any] | None:
    """A client frame as a JSON object, or None for binary, invalid or non-object frames."""
    text = message.get("text")
    if text is None:
        return None
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


# ── Connection phases ───────────────────────────────────────────────────


async def _authenticate(websocket: WebSocket, bind: SessionBind) -> Subscriber:
    """Wait for the auth message and turn it into a `Subscriber`.

    Raises:
        _CloseSocket: no auth message in time, a malformed one, or a refused token.
        WebSocketDisconnect: the client left before authenticating.
    """
    try:
        message = await asyncio.wait_for(websocket.receive(), AUTH_TIMEOUT_SECONDS)
    except TimeoutError as exc:
        raise _CloseSocket(CloseCode.AUTH_TIMEOUT, "auth_timeout") from exc
    if message["type"] == "websocket.disconnect":
        raise WebSocketDisconnect(code=message.get("code", 1000))

    payload = _parse_client_message(message)
    token = payload.get("token") if payload is not None and payload.get("type") == "auth" else None
    if not isinstance(token, str) or not token:
        raise _CloseSocket(CloseCode.PROTOCOL_ERROR, "expected_auth_message")

    try:
        return await realtime_service.authenticate(bind, token)
    except UnauthorizedError as exc:
        raise _CloseSocket(CloseCode.UNAUTHORIZED, "unauthorized") from exc
    except ForbiddenError as exc:
        raise _CloseSocket(CloseCode.FORBIDDEN, "forbidden") from exc
    except Exception as exc:
        logger.exception("Dashboard socket authentication failed unexpectedly")
        raise _CloseSocket(CloseCode.INTERNAL_ERROR, "internal_error") from exc


async def _subscribe(redis: Redis | None, subscriber: Subscriber) -> tuple[PubSub | None, list[str]]:
    """Open this socket's pub/sub subscription and wait until Redis confirms it.

    Returns the subscription (None when there is nothing to hear) and any
    events that arrived while confirmations were still coming in, to be sent
    straight after "ready".

    Waiting for the confirmations is what makes "ready" true. redis-py's
    `subscribe()` only writes the command; a client that refetched the REST API
    on "ready" and then missed an event published a moment later would have a
    silent gap in its picture.

    Raises:
        _CloseSocket: Redis is absent, unreachable or did not confirm in time.
            The socket is closed rather than left open and silent — a dashboard
            that believes it is live but receives nothing is worse than one
            that knows to retry.
    """
    channels, patterns = subscriber.channels, subscriber.patterns
    if not channels and not patterns:
        return None, []
    if redis is None:
        raise _CloseSocket(CloseCode.TRY_AGAIN_LATER, "realtime_unavailable")

    pubsub = redis.pubsub()
    early: list[str] = []
    try:
        if channels:
            await pubsub.subscribe(*channels)
        if patterns:
            await pubsub.psubscribe(*patterns)
        await _await_confirmations(pubsub, len(channels) + len(patterns), early)
    except Exception as exc:
        logger.warning("Dashboard socket could not subscribe: Redis unavailable.", exc_info=True)
        with contextlib.suppress(Exception):
            await pubsub.aclose()
        raise _CloseSocket(CloseCode.TRY_AGAIN_LATER, "realtime_unavailable") from exc
    return pubsub, early


async def _await_confirmations(pubsub: PubSub, expected: int, early: list[str]) -> None:
    """Read until Redis has confirmed `expected` (p)subscriptions; keep any events seen meanwhile.

    Raises:
        TimeoutError: not all confirmed within `SUBSCRIBE_TIMEOUT_SECONDS`.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + SUBSCRIBE_TIMEOUT_SECONDS
    confirmed = 0
    while confirmed < expected:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError(f"Redis confirmed {confirmed} of {expected} subscriptions")
        message = await pubsub.get_message(timeout=min(POLL_INTERVAL_SECONDS, remaining))
        if message is None:
            continue
        if message["type"] in ("subscribe", "psubscribe"):
            confirmed += 1
        elif message["type"] in ("message", "pmessage"):
            early.append(message["data"])


async def _revalidate(bind: SessionBind, subscriber: Subscriber) -> None:
    """Re-read the account; close the socket if what it may hear has changed."""
    try:
        current = await realtime_service.load_subscriber(
            bind, user_id=subscriber.user_id, expires_at=subscriber.expires_at
        )
    except UnauthorizedError as exc:
        raise _CloseSocket(CloseCode.UNAUTHORIZED, "account_deactivated") from exc
    except ForbiddenError as exc:
        raise _CloseSocket(CloseCode.FORBIDDEN, "forbidden") from exc
    except Exception as exc:
        # Cannot confirm the account is still entitled to the feed. Closing is
        # the conservative answer; the client reconnects with backoff.
        logger.warning("Dashboard socket revalidation failed for user_id=%s", subscriber.user_id, exc_info=True)
        raise _CloseSocket(CloseCode.TRY_AGAIN_LATER, "revalidation_failed") from exc
    if not current.same_scope_as(subscriber):
        # Resubscribing in place would race the pump reading the same pub/sub
        # connection. A reconnect is simpler and the client handles it anyway.
        raise _CloseSocket(CloseCode.SUBSCRIPTION_CHANGED, "subscription_changed")


async def _pump(sender: _Sender, pubsub: PubSub | None, subscriber: Subscriber, bind: SessionBind) -> None:
    """Forward events, send heartbeats, enforce expiry and revalidate. Runs until closed.

    Raises:
        _CloseSocket: token expiry, revalidation failure, or Redis lost.
    """
    loop = asyncio.get_running_loop()
    last_heartbeat = last_revalidated = loop.time()

    while True:
        remaining = (subscriber.expires_at - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            raise _CloseSocket(CloseCode.UNAUTHORIZED, "token_expired")
        wait = min(POLL_INTERVAL_SECONDS, HEARTBEAT_INTERVAL_SECONDS, remaining)

        message = None
        if pubsub is None:
            await asyncio.sleep(wait)
        else:
            try:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=wait)
            except Exception as exc:
                # The pub/sub connection is gone, and events published
                # meanwhile are gone with it: Redis does not buffer for absent
                # subscribers. So the client must reconnect and refetch rather
                # than carry on believing it has seen everything.
                logger.warning("Dashboard socket lost its Redis subscription.", exc_info=True)
                raise _CloseSocket(CloseCode.TRY_AGAIN_LATER, "realtime_unavailable") from exc

        if message is not None and message.get("type") in ("message", "pmessage"):
            # Already the JSON envelope, produced by `app.core.events`. Passed
            # through as-is rather than decoded and re-encoded per subscriber.
            await sender.text(message["data"])

        now = loop.time()
        if now - last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
            await sender.json({"type": "ping", "sent_at": _utc_iso(datetime.now(UTC))})
            last_heartbeat = now
        if now - last_revalidated >= REVALIDATE_INTERVAL_SECONDS:
            await _revalidate(bind, subscriber)
            last_revalidated = loop.time()


async def _read_client(websocket: WebSocket, sender: _Sender) -> None:
    """Answer client pings; ignore anything else. Returns when the client disconnects.

    Unknown messages are ignored rather than fatal, so a newer client talking
    to an older server degrades instead of disconnecting.
    """
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return
        payload = _parse_client_message(message)
        if payload is not None and payload.get("type") == "ping":
            await sender.json({"type": "pong"})


async def _run(
    websocket: WebSocket, sender: _Sender, pubsub: PubSub | None, subscriber: Subscriber, bind: SessionBind
) -> None:
    """Run the pump and the reader until either ends, then stop the other."""
    tasks = [
        asyncio.create_task(_pump(sender, pubsub, subscriber, bind)),
        asyncio.create_task(_read_client(websocket, sender)),
    ]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        # Also reached when this handler is itself cancelled (server shutdown):
        # neither task may outlive the connection.
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    for task in done:
        exc = task.exception()
        if isinstance(exc, _CloseSocket):
            raise exc
        if exc is not None and not isinstance(exc, WebSocketDisconnect):
            # Typically a send on a socket the client has just dropped.
            logger.debug("Dashboard socket ended: %r", exc)


async def _close(websocket: WebSocket, close: _CloseSocket) -> None:
    """Close with a code, tolerating a client that has already gone."""
    with contextlib.suppress(Exception):
        await websocket.close(code=close.code, reason=close.reason)


# ── Route ───────────────────────────────────────────────────────────────


@router.websocket("/dashboard")
async def dashboard_feed(websocket: WebSocket, db: DBSession) -> None:
    """Realtime issue events for authority staff. Protocol: `docs/realtime.md`.

    `db` is used only for its bind: every lookup opens its own short session
    (`realtime_service._session_for`), so a long-lived socket never pins a
    pooled database connection.
    """
    await websocket.accept()
    bind = db.bind

    try:
        subscriber = await _authenticate(websocket, bind)
    except WebSocketDisconnect:
        return
    except _CloseSocket as close:
        await _close(websocket, close)
        return

    pubsub: PubSub | None = None
    try:
        pubsub, early = await _subscribe(getattr(websocket.app.state, "redis", None), subscriber)
        sender = _Sender(websocket)
        await sender.json(
            {
                "type": "ready",
                "user_id": str(subscriber.user_id),
                "role": subscriber.role,
                "all_zones": subscriber.all_zones,
                "zone_ids": sorted(str(z) for z in subscriber.zone_ids),
                "expires_at": _utc_iso(subscriber.expires_at),
                "heartbeat_interval_seconds": HEARTBEAT_INTERVAL_SECONDS,
            }
        )
        for data in early:
            await sender.text(data)
        logger.info(
            "Dashboard socket ready user_id=%s role=%s zones=%s",
            subscriber.user_id,
            subscriber.role,
            "all" if subscriber.all_zones else len(subscriber.zone_ids),
        )
        await _run(websocket, sender, pubsub, subscriber, bind)
    except _CloseSocket as close:
        logger.info("Dashboard socket closing user_id=%s code=%d %s", subscriber.user_id, close.code, close.reason)
        await _close(websocket, close)
    except WebSocketDisconnect:
        pass
    except Exception:
        # In practice: the client dropped between subscribing and "ready".
        logger.warning("Dashboard socket for user_id=%s ended with an error.", subscriber.user_id, exc_info=True)
    finally:
        if pubsub is not None:
            # Closes the pub/sub connection, which drops its subscriptions on
            # the Redis side too. Never let cleanup failure escape.
            try:
                await pubsub.aclose()
            except Exception:
                logger.warning("Error while closing a dashboard pub/sub connection.", exc_info=True)
