from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.config import settings
from app.core.exceptions import UnauthorizedError

logger = logging.getLogger(__name__)

# ── Password Hashing ────────────────────────────────────────────────────
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    """Hash a plaintext password using bcrypt."""
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plaintext password against a bcrypt hash."""
    return pwd_context.verify(plain_password, hashed_password)


# ── JWT Token Management (RS256) ────────────────────────────────────────


def create_access_token(
    user_id: str,
    role: str,
    email: str | None = None,
) -> str:
    """Create a JWT access token signed with RS256."""
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": user_id,
        "role": role,
        "iat": now,
        "exp": now + timedelta(minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES),
        "jti": secrets.token_hex(16),
    }
    if email:
        payload["email"] = email

    return jwt.encode(
        payload,
        settings.JWT_PRIVATE_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )


def create_refresh_token() -> tuple[str, str]:
    """Generate a cryptographic refresh token and its SHA-256 hash.

    Returns:
        Tuple of (raw_token, token_hash). The raw token is sent to the client
        once. Only the hash is stored in the database.
    """
    raw_token = secrets.token_urlsafe(64)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    return raw_token, token_hash


def decode_jwt(token: str) -> dict[str, Any]:
    """Decode and validate a JWT access token.

    Raises:
        UnauthorizedError: If the token is invalid or expired.
    """
    try:
        payload = jwt.decode(
            token,
            settings.JWT_PUBLIC_KEY,
            algorithms=[settings.JWT_ALGORITHM],
        )
        if "sub" not in payload:
            raise UnauthorizedError("Invalid token: missing subject claim")
        return payload
    except JWTError as exc:
        raise UnauthorizedError(f"Invalid or expired token: {exc}") from exc


def hash_refresh_token(raw_token: str) -> str:
    """Compute SHA-256 hash of a raw refresh token for DB lookup."""
    return hashlib.sha256(raw_token.encode()).hexdigest()
