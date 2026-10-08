"""Auth business logic — registration, login, refresh rotation, revocation.

The routers stay thin: they translate HTTP into these calls and back. Everything
that touches the database or credential material lives here.

Three properties this module exists to guarantee:

* **Only hashes are stored.** Passwords are bcrypt-hashed; refresh tokens are
  stored as their SHA-256 hash. The raw refresh token exists exactly once, in
  the response that issues it, and is never written to the database or a log.
* **Login does not leak account existence.** An unknown email runs the same
  bcrypt verification as a wrong password — against a fixed dummy hash — and
  produces a byte-identical response, so neither the body nor the response time
  distinguishes the two.
* **Refresh rotates atomically.** The presented token is revoked in the same
  transaction that inserts its replacement, so a token is never valid twice.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.exceptions import ConflictError, UnauthorizedError
from app.core.permissions import Role
from app.core.security import (
    create_access_token,
    create_refresh_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.schemas.auth import RegisterRequest

# A bcrypt hash of a throwaway random string, generated at the same cost factor
# (12) as every real password hash. Verifying a submitted password against this
# when no account matches makes the unknown-email path cost the same ~180ms as
# the wrong-password path. Nothing can authenticate against it: the plaintext
# was discarded, and it is never attached to a user row.
DUMMY_PASSWORD_HASH = "$2b$12$02unpopcRxN0fjEkduD8eOvQ8hxFJwIufpMdvZNWz1ZuXfIxx7hdS"

# One message for every login failure — wrong password, unknown email, or a
# deactivated account. Anything more specific is an account-enumeration oracle.
INVALID_CREDENTIALS_MESSAGE = "Invalid email or password"

# Likewise for refresh: unknown, expired, revoked and belonging-to-a-disabled-user
# all collapse into one response.
INVALID_REFRESH_MESSAGE = "Invalid or expired refresh token"


def access_token_ttl_seconds() -> int:
    """Lifetime of an issued access token, in seconds (the `expires_in` field)."""
    return settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60


def _refresh_token_expiry() -> datetime:
    """Absolute expiry timestamp for a newly issued refresh token."""
    return datetime.now(UTC) + timedelta(days=settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS)


def _registration_conflict(exc: IntegrityError) -> ConflictError:
    """Map a unique-violation on insert to the right 409.

    Only reached when two registrations race past the pre-checks; the constraint
    name in the driver error says which column collided.
    """
    if "phone" in str(exc.orig):
        return ConflictError(code="PHONE_ALREADY_REGISTERED", message="Phone number already registered")
    return ConflictError(code="EMAIL_ALREADY_REGISTERED", message="Email already registered")


# ── Registration ────────────────────────────────────────────────────────


async def register_citizen(db: AsyncSession, payload: RegisterRequest) -> User:
    """Create a citizen account from an email + password registration.

    The email arrives already lowercased and stripped by `RegisterRequest`, so
    account lookups are case-insensitive without a functional index.

    Raises:
        ConflictError: The email (or phone, when supplied) is already taken.
    """
    if await db.scalar(select(User.id).where(User.email == payload.email)) is not None:
        raise ConflictError(code="EMAIL_ALREADY_REGISTERED", message="Email already registered")

    if payload.phone is not None and await db.scalar(select(User.id).where(User.phone == payload.phone)) is not None:
        raise ConflictError(code="PHONE_ALREADY_REGISTERED", message="Phone number already registered")

    user = User(
        email=payload.email,
        phone=payload.phone,
        name=payload.name,
        password_hash=hash_password(payload.password),
        role=Role.CITIZEN.value,
        is_anonymous=False,
    )
    db.add(user)
    try:
        # flush(), not commit(): the request-scoped session owns the transaction
        # boundary (app/database.py:get_db), and tests roll it back.
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise _registration_conflict(exc) from exc

    return user


# ── Login ───────────────────────────────────────────────────────────────


async def authenticate_user(db: AsyncSession, email: str, password: str) -> User:
    """Return the user matching these credentials, or raise a 401.

    Deliberately branch-free until the very end: every path performs exactly one
    bcrypt verification and every failure raises the identical error, so neither
    the response nor its timing reveals whether the account exists.

    Raises:
        UnauthorizedError: No such account, wrong password, or account disabled.
    """
    user = await db.scalar(select(User).where(User.email == email))

    stored_hash = user.password_hash if user is not None and user.password_hash else DUMMY_PASSWORD_HASH
    password_matches = verify_password(password, stored_hash)

    if user is None or not user.password_hash or not password_matches or not user.is_active:
        raise UnauthorizedError(INVALID_CREDENTIALS_MESSAGE)

    return user


# ── Token issuance and rotation ─────────────────────────────────────────


async def issue_token_pair(db: AsyncSession, user: User) -> tuple[str, str]:
    """Mint an access token and a refresh token for `user`.

    Returns:
        `(access_token, raw_refresh_token)`. Only the SHA-256 hash of the
        refresh token is persisted — the raw value returned here is the one and
        only time it exists outside the client.
    """
    access_token = create_access_token(user_id=str(user.id), role=user.role, email=user.email)
    raw_refresh_token, token_hash = create_refresh_token()

    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=token_hash,
            expires_at=_refresh_token_expiry(),
            is_revoked=False,
        )
    )
    await db.flush()

    return access_token, raw_refresh_token


async def rotate_refresh_token(db: AsyncSession, raw_token: str) -> tuple[str, str]:
    """Exchange a refresh token for a fresh pair, revoking the one presented.

    The revocation and the replacement are the same transaction, so the old
    token stops working the instant the new one starts. The row is locked
    FOR UPDATE so two concurrent refreshes with the same token serialise rather
    than both succeeding.

    Raises:
        UnauthorizedError: Token unknown, expired, already revoked, or its owner
            has been deactivated.
    """
    stored = await db.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(raw_token)).with_for_update()
    )
    if stored is None or stored.is_revoked or stored.expires_at <= datetime.now(UTC):
        raise UnauthorizedError(INVALID_REFRESH_MESSAGE)

    user = await db.get(User, stored.user_id)
    if user is None or not user.is_active:
        raise UnauthorizedError(INVALID_REFRESH_MESSAGE)

    stored.is_revoked = True
    return await issue_token_pair(db, user)


async def revoke_refresh_token(db: AsyncSession, raw_token: str | None) -> None:
    """Revoke a refresh token if it exists; do nothing otherwise.

    Intentionally silent about the outcome. Logout must be idempotent and must
    not become an oracle for which tokens exist, so an unknown token, an
    already-revoked token and an absent token are all indistinguishable to the
    caller.
    """
    if not raw_token:
        return

    await db.execute(
        update(RefreshToken)
        .where(
            RefreshToken.token_hash == hash_refresh_token(raw_token),
            RefreshToken.is_revoked.is_(False),
        )
        .values(is_revoked=True)
    )
