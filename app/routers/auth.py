"""Auth router — email/password registration, login, token rotation, logout.

Five real endpoints (`register`, `login`, `refresh`, `logout`, plus
`GET /users/me` in the users router) back the MVP demo loop. The two OAuth
routes remain Phase 0 mock stubs: Google and Apple sign-in were cut from the
MVP, and the stubs are kept so the contract's surface stays intact.

Every handler here is a translation layer — validate, delegate to
`app.services.auth_service`, shape the response. No credential logic lives in
this module, and nothing logs an email, a password, a hash or a token.

Refresh tokens travel in the JSON body only. No cookie is read or emitted
anywhere in this module. The `weft_refresh` httpOnly cookie fallback that
`openapi.yaml` used to describe was reversed in the MVP pivot; the contract has
since been corrected to match, and `tests/unit/test_contract_drift.py` now
asserts that no cookie language can reappear in it.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, status

from app.core.exceptions import BadRequestError
from app.dependencies import DBSession, enforce_login_rate_limit
from app.schemas.auth import (
    AuthResponse,
    LoginRequest,
    LogoutRequest,
    RefreshTokenRequest,
    RegisterRequest,
    RegisterResponse,
    TokenResponse,
)
from app.schemas.user import UserProfile
from app.services import auth_service

logger = logging.getLogger(__name__)

router = APIRouter()


# ── POST /auth/register ─────────────────────────────────────────────────


@router.post(
    "/register",
    response_model=RegisterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new citizen account",
)
async def register(payload: RegisterRequest, db: DBSession) -> RegisterResponse:
    """Create a citizen account. Returns the profile, not tokens — the client logs in next."""
    user = await auth_service.register_citizen(db, payload)
    logger.info("Registered citizen account user_id=%s", user.id)
    return RegisterResponse(
        user_id=user.id,
        email=user.email,
        name=user.name,
        role=user.role,
        created_at=user.created_at,
    )


# ── POST /auth/login ────────────────────────────────────────────────────


@router.post(
    "/login",
    response_model=AuthResponse,
    # Route-level, so it resolves before the handler and before any password
    # verification: failed attempts are exactly what needs counting. 5/min/IP
    # per TRD Section 6; fails open when Redis is down — see the dependency.
    dependencies=[Depends(enforce_login_rate_limit)],
    summary="Login with email + password",
)
async def login(payload: LoginRequest, db: DBSession) -> AuthResponse:
    """Exchange email + password for an access token, a refresh token and the profile."""
    user = await auth_service.authenticate_user(db, payload.email, payload.password)
    access_token, refresh_token = await auth_service.issue_token_pair(db, user)
    logger.info("Login succeeded user_id=%s", user.id)

    return AuthResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=auth_service.access_token_ttl_seconds(),
        user=UserProfile.model_validate(user),
    )


# ── POST /auth/oauth/google ─────────────────────────────────────────────


@router.post("/oauth/google")
async def google_oauth_mock() -> dict:
    """Mock: Google OAuth sign-in.

    Still a Phase 0 stub. Google sign-in is out of scope for the MVP; this route
    is kept so the contract's surface does not shrink.
    """
    return {
        "access_token": "mock.jwt.google.token",
        "refresh_token": "mock.refresh.google.token",
        "token_type": "bearer",
        "expires_in": 86400,
        "user": {
            "id": "550e8400-e29b-41d4-a716-446655440002",
            "email": "google.user@gmail.com",
            "name": "Google User",
            "role": "CITIZEN",
        },
    }


# ── POST /auth/oauth/apple ──────────────────────────────────────────────


@router.post("/oauth/apple")
async def apple_oauth_mock() -> dict:
    """Mock: Apple OAuth sign-in.

    Still a Phase 0 stub, for the same reason as the Google route.
    """
    return {
        "access_token": "mock.jwt.apple.token",
        "refresh_token": "mock.refresh.apple.token",
        "token_type": "bearer",
        "expires_in": 86400,
        "user": {
            "id": "550e8400-e29b-41d4-a716-446655440003",
            "email": "apple.user@icloud.com",
            "name": "Apple User",
            "role": "CITIZEN",
        },
    }


# ── POST /auth/refresh ──────────────────────────────────────────────────


@router.post("/refresh", response_model=TokenResponse, summary="Rotate refresh token")
async def refresh(db: DBSession, payload: RefreshTokenRequest | None = None) -> TokenResponse:
    """Exchange a refresh token for a new pair; the presented token is revoked.

    Both tokens are always returned: clients replace the stored refresh token on
    every rotation, so omitting it would silently end the session.
    """
    raw_token = payload.refresh_token if payload else None
    if not raw_token:
        raise BadRequestError(code="MISSING_REFRESH_TOKEN", message="refresh_token is required")

    access_token, new_refresh_token = await auth_service.rotate_refresh_token(db, raw_token)

    return TokenResponse(
        access_token=access_token,
        refresh_token=new_refresh_token,
        expires_in=auth_service.access_token_ttl_seconds(),
    )


# ── POST /auth/logout ───────────────────────────────────────────────────


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    # Explicit `None`: FastAPI otherwise infers the response model from the
    # `-> None` return annotation, which `from __future__ import annotations`
    # resolves to `NoneType` — a truthy class — and then asserts that a 204 must
    # not have a response body, at import time. Without this the whole app fails
    # to import.
    response_model=None,
    summary="Revoke a refresh token (single device)",
)
async def logout(db: DBSession, payload: LogoutRequest | None = None) -> None:
    """Revoke one refresh token, ending the session on that device only.

    Authenticated by the refresh token itself rather than by a bearer access
    token: possession of the 64-byte secret is the proof, and requiring a valid
    access token would make an expired session impossible to revoke — exactly
    the case where revocation matters. `openapi.yaml` documents it that way —
    no `security` requirement on this operation — and a contract-drift test
    pins it, because reinstating `BearerAuth` here would quietly make expired
    sessions unrevocable.

    Idempotent by construction: unknown, already-revoked and absent tokens all
    return 204, so the response is never an oracle for which tokens exist.
    """
    await auth_service.revoke_refresh_token(db, payload.refresh_token if payload else None)
