from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from redis.asyncio import ConnectionPool, Redis

from app.config import settings
from app.core.exceptions import WeftException, weft_exception_handler
from app.routers import admin, analytics, auth, departments, issues, notifications, upvotes, users, websocket
from app.schemas.common import HealthResponse

logger = logging.getLogger(__name__)

# Bound how long a request may wait on Redis. Without these a dead-but-routable
# Redis host makes every login hang on the OS TCP timeout, which turns the
# fail-open rate limiter (see app/dependencies.py) into a fail-slow one.
REDIS_CONNECT_TIMEOUT_SECONDS = 1.0
REDIS_COMMAND_TIMEOUT_SECONDS = 1.0


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan: startup and shutdown events.

    Owns the process-wide Redis connection pool, the same way `app/database.py`
    owns the SQLAlchemy engine: created once, shared by every request through
    `app.dependencies.get_redis`, disposed on shutdown.

    Startup never fails on Redis. `Redis(...)` opens no socket, so an unreachable
    cache surfaces at first command, where the caller's fail-open path handles
    it; and a malformed `REDIS_URL` is caught here and downgraded to "no client"
    rather than taking the API down.
    """
    logger.info("Weft API starting up — ENV=%s", settings.ENV)

    pool: ConnectionPool | None = None
    try:
        pool = ConnectionPool.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=REDIS_CONNECT_TIMEOUT_SECONDS,
            socket_timeout=REDIS_COMMAND_TIMEOUT_SECONDS,
        )
        app.state.redis = Redis(connection_pool=pool)
    except Exception:
        # Availability over strictness, as with the limiter itself: no cache is
        # a degraded API, not a dead one.
        logger.warning("Could not create the Redis connection pool; Redis-backed features are off.", exc_info=True)
        app.state.redis = None

    try:
        yield
    finally:
        client, app.state.redis = app.state.redis, None
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                logger.warning("Error while closing the Redis client.", exc_info=True)
        if pool is not None:
            try:
                await pool.disconnect()
            except Exception:
                logger.warning("Error while disconnecting the Redis connection pool.", exc_info=True)
        logger.info("Weft API shutting down")


def create_app() -> FastAPI:
    """FastAPI application factory."""
    app = FastAPI(
        title=settings.APP_NAME,
        version=settings.APP_VERSION,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    # Declared up front so `get_redis` reads a real attribute rather than
    # relying on its fallback. An ASGI test client that never runs lifespan
    # therefore sees an explicit "no Redis" instead of a missing attribute.
    app.state.redis = None

    # ── CORS Middleware ──────────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Exception Handlers ───────────────────────────────────────────────
    app.add_exception_handler(WeftException, weft_exception_handler)

    # ── Health Check ─────────────────────────────────────────────────────
    @app.get("/health", response_model=HealthResponse, tags=["Health"])
    async def health() -> HealthResponse:
        """Application health check endpoint."""
        return HealthResponse(
            status="ok",
            version=settings.APP_VERSION,
            env=settings.ENV,
        )

    # ── Register Routers ────────────────────────────────────────────────
    api_prefix = settings.API_V1_PREFIX

    # ── Media (local-disk storage stand-in for S3/CloudFront) ────────────
    # Dev/self-hosted only. In production CloudFront serves the S3 bucket and
    # this mount does nothing, because MEDIA_ROOT is not where images live.
    # Mounted before the routers so a media URL never falls through to a route.
    media_root = Path(settings.MEDIA_ROOT)
    media_root.mkdir(parents=True, exist_ok=True)
    app.mount(settings.MEDIA_BASE_URL, StaticFiles(directory=media_root), name="media")

    app.include_router(auth.router, prefix=f"{api_prefix}/auth", tags=["Auth"])
    app.include_router(users.router, prefix=f"{api_prefix}/users", tags=["Users"])
    app.include_router(issues.router, prefix=f"{api_prefix}/issues", tags=["Issues"])
    app.include_router(upvotes.router, prefix=f"{api_prefix}/issues", tags=["Upvotes"])
    app.include_router(notifications.router, prefix=f"{api_prefix}/notifications", tags=["Notifications"])
    app.include_router(analytics.router, prefix=f"{api_prefix}/analytics", tags=["Analytics"])
    app.include_router(admin.router, prefix=f"{api_prefix}/admin", tags=["Admin"])
    app.include_router(departments.router, prefix=f"{api_prefix}/departments", tags=["Departments"])
    app.include_router(websocket.router, prefix=f"{api_prefix}/ws", tags=["WebSocket"])

    return app


app = create_app()
