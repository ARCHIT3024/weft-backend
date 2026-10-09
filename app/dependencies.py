"""Shared FastAPI dependencies: the database session, the bearer token, Redis,
and the login and issue-submission rate limits that Redis backs.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import OAuth2PasswordBearer
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import RateLimitError
from app.core.rate_limiter import SlidingWindowRateLimiter
from app.database import get_db

logger = logging.getLogger(__name__)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/v1/auth/login", auto_error=False)


async def get_redis(request: Request) -> Redis | None:
    """Yield the shared Redis client, or `None` when there isn't one.

    The client and its connection pool are built once in `app.main.lifespan` and
    parked on `app.state`; this is the read side, mirroring how `get_db` hands
    out sessions from the single engine in `app/database.py`. Nothing is opened
    or closed per request — the pool is the point.

    Returns `None` rather than raising when the pool was never created (Redis
    unreachable at startup, or an ASGI test client that skips lifespan). Every
    caller must therefore treat `None` as "no cache available" and degrade, not
    fail. See `enforce_login_rate_limit`.
    """
    return getattr(request.app.state, "redis", None)


# Type aliases for cleaner dependency injection
DBSession = Annotated[AsyncSession, Depends(get_db)]
OptionalToken = Annotated[str | None, Depends(oauth2_scheme)]
RedisClient = Annotated[Redis | None, Depends(get_redis)]


# ── Login rate limit ────────────────────────────────────────────────────

# TRD Section 6 and implementation-plan task 1.3: 5 login attempts per minute
# per client IP. The issue-submission budgets are further down. The fourth
# documented budget, 500/minute/IP global, is deliberately not wired anywhere
# in the application: it belongs at the edge (ALB/WAF rate-based rule, task
# 5.8), where it costs nothing per request and sees the real client address —
# see `client_ip` for why this process cannot.
LOGIN_RATE_LIMIT = 5
LOGIN_RATE_LIMIT_WINDOW_SECONDS = 60
LOGIN_RATE_LIMIT_KEY_PREFIX = "rate_limit:login"


def client_ip(request: Request) -> str:
    """Best-effort client identity for rate-limit keys.

    `request.client` is the socket peer, which is the right answer today: the
    API is reached directly. Behind the planned ALB/CloudFront (DECISIONS.md,
    "Cloud: AWS (ECS Fargate, RDS, S3, CloudFront)") every peer becomes the load
    balancer, and this one value would bucket the entire internet into a single
    budget — 5 logins a minute, 3 anonymous reports an hour, for everyone — a
    self-inflicted outage. That deployment needs
    `X-Forwarded-For` parsing with a *trusted proxy count*, because the header is
    client-writable and a naive left-most read hands every attacker a free key
    rotation. Deliberately not built now; this function is the seam, so it is
    one place to change rather than a grep.

    `request.client` is `None` for some ASGI transports, hence the fallback.
    """
    return request.client.host if request.client else "unknown"


async def enforce_login_rate_limit(request: Request, redis: RedisClient) -> None:
    """Cap login attempts at 5/minute/IP. Fails open.

    Attached to `POST /auth/login` only, as a route-level dependency so it runs
    before the handler — and therefore before any password verification — and
    counts failed attempts, which is the whole point of a brute-force cap.

    Deliberate availability-over-strictness tradeoff: if Redis is missing,
    unreachable, or errors mid-command, this logs a warning and lets the request
    through. It was chosen, not overlooked. Login is the front door of the
    product; a cache outage must not lock every citizen out of the app, and the
    downside — a brute-force window that is open exactly as long as Redis is
    down — is bounded and monitorable. Same shape as the non-blocking AI
    fallback in DECISIONS.md D-2. Reverse this only with a deliberate decision
    record; do not flip it by accident while "cleaning up" the except clause.

    Raises:
        RateLimitError: 429 with `limit`, `window` and `retry_after_seconds`,
            when the caller has already spent its budget for the window.
    """
    if redis is None:
        logger.warning("Login rate limiting is inactive: no Redis client available. Allowing the request.")
        return

    limiter = SlidingWindowRateLimiter(redis)
    key = f"{LOGIN_RATE_LIMIT_KEY_PREFIX}:{client_ip(request)}"

    try:
        await limiter.check_rate_limit(key, LOGIN_RATE_LIMIT, LOGIN_RATE_LIMIT_WINDOW_SECONDS)
    except RateLimitError:
        # The limit did its job — this one propagates as the contracted 429.
        raise
    except Exception:
        # Anything else is Redis failing, not the caller misbehaving. Fail open.
        logger.warning("Login rate limit check failed; allowing the request (fail-open).", exc_info=True)


# ── Issue-submission rate limit ─────────────────────────────────────────

# TRD Section 6 and implementation-plan task 1.9: 10 reports per hour for a
# signed-in user, 3 per hour per client IP for an anonymous one. The TRD keys
# the anonymous budget "per device"; there is no device identity an anonymous
# caller cannot forge, so the IP stands in for it, with `client_ip`'s caveat.
ISSUE_RATE_LIMIT_REGISTERED = 10
ISSUE_RATE_LIMIT_ANONYMOUS = 3
ISSUE_RATE_LIMIT_WINDOW_SECONDS = 3600
ISSUE_RATE_LIMIT_KEY_PREFIX = "rate_limit:issues"


async def enforce_issue_rate_limit(
    request: Request,
    redis: RedisClient,
    token: OptionalToken,
    db: DBSession,
) -> None:
    """Cap `POST /issues` at 10/hour/user, or 3/hour/IP when anonymous. Fails open.

    Attached to the route as a route-level dependency, like the login limit, so
    a refused submission never reaches CAPTCHA verification, image decoding or
    the database insert.

    The caller is identified exactly as the handler identifies them —
    `get_current_user_optional` — so a missing, invalid or deactivated token is
    anonymous for the budget too, and spends the per-IP allowance rather than a
    per-user one. A signed-in user is keyed by user id, not IP: a household or
    an office behind one NAT must not share ten reports an hour.

    Fails open on a Redis outage, for the reason `enforce_login_rate_limit`
    gives and with the same structure: a cache outage must not stop citizens
    reporting hazards. The same warning applies — `RateLimitError` is re-raised
    before the broad catch; do not fold the two together while tidying.

    Raises:
        RateLimitError: 429 with a `Retry-After` header and `limit`, `window`
            and `retry_after_seconds` in the error body.
    """
    if redis is None:
        logger.warning("Issue rate limiting is inactive: no Redis client available. Allowing the request.")
        return

    # Imported here, not at module level: `app.core.permissions` imports this
    # module for `oauth2_scheme`, so a top-level import would be circular.
    # Called directly rather than declared as a dependency for the same reason;
    # `db` is the request's cached session, so the user lookup the handler
    # repeats is answered from the session's identity map.
    from app.core.permissions import get_current_user_optional

    user = await get_current_user_optional(token=token, db=db)
    if user is not None:
        key = f"{ISSUE_RATE_LIMIT_KEY_PREFIX}:user:{user.id}"
        limit = ISSUE_RATE_LIMIT_REGISTERED
    else:
        key = f"{ISSUE_RATE_LIMIT_KEY_PREFIX}:ip:{client_ip(request)}"
        limit = ISSUE_RATE_LIMIT_ANONYMOUS

    limiter = SlidingWindowRateLimiter(redis)
    try:
        await limiter.check_rate_limit(key, limit, ISSUE_RATE_LIMIT_WINDOW_SECONDS)
    except RateLimitError:
        # The limit did its job — this one propagates as the contracted 429.
        raise
    except Exception:
        # Anything else is Redis failing, not the caller misbehaving. Fail open.
        logger.warning("Issue rate limit check failed; allowing the request (fail-open).", exc_info=True)
