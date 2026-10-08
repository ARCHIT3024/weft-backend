"""Auth schemas — registration, login, OAuth, token rotation.

Derived from the frozen OpenAPI contract (`openapi.yaml`): component schemas
`RegisterRequest`, `RegisterResponse`, `LoginRequest`, `AuthResponse` and
`TokenResponse`, plus the inline request bodies of `googleOAuth`, `appleOAuth`
and `refreshToken`.

Credential material (passwords, password hashes) appears only on request
models. The `refresh_token` field on the response models is the token-issuance
channel mandated by the contract, not a leak of stored state — the server
persists only a SHA-256 hash of it (TRD Section 6).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.schemas.user import UserProfile, UserRole

# Pragmatic email shape check. The contract declares `format: email`, which
# would normally map to `pydantic.EmailStr`, but the `email-validator`
# dependency is not pinned in requirements.txt; `json_schema_extra={"format":
# "email"}` on each field keeps the emitted OpenAPI schema faithful to the
# contract in the meantime.
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
EMAIL_MAX_LENGTH = 255
PASSWORD_MIN_LENGTH = 8

EmailAddress = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        to_lower=True,
        max_length=EMAIL_MAX_LENGTH,
        pattern=EMAIL_PATTERN,
    ),
]
"""Email address, normalised to lowercase so account lookups are case-insensitive."""


# ── Enums ───────────────────────────────────────────────────────────────


class TokenType(StrEnum):
    """OAuth 2.0 token type returned alongside an access token."""

    BEARER = "bearer"


# ── Requests ────────────────────────────────────────────────────────────


class RegisterRequest(BaseModel):
    """Email + password registration payload for a citizen account.

    Mirrors `components.schemas.RegisterRequest`.
    """

    email: EmailAddress = Field(
        json_schema_extra={"format": "email"},
        description="Email address; must be unique across accounts",
        examples=["citizen@example.com"],
    )
    password: str = Field(
        min_length=PASSWORD_MIN_LENGTH,
        description=f"Plaintext password, minimum {PASSWORD_MIN_LENGTH} characters; bcrypt-hashed before storage",
    )
    name: str = Field(
        max_length=100,
        description="Display name",
        examples=["Test Citizen"],
    )
    phone: str | None = Field(
        default=None,
        max_length=20,
        description="Optional phone number; must be unique across accounts when supplied",
    )


class LoginRequest(BaseModel):
    """Email + password login payload.

    Mirrors `components.schemas.LoginRequest`. No password length constraint is
    applied on login so that legacy or temporary credentials still authenticate.
    """

    email: EmailAddress = Field(
        json_schema_extra={"format": "email"},
        description="Registered email address",
        examples=["citizen@example.com"],
    )
    password: str = Field(description="Plaintext password")


class GoogleOAuthRequest(BaseModel):
    """Google sign-in payload.

    Mirrors the inline request body of the `googleOAuth` operation.
    """

    id_token: str = Field(description="Google ID token from mobile/web SDK")


class AppleOAuthRequest(BaseModel):
    """Apple sign-in payload.

    Mirrors the inline request body of the `appleOAuth` operation.
    """

    authorization_code: str = Field(description="Apple authorization code from the Sign in with Apple flow")
    name: str | None = Field(
        default=None,
        description="Full name (Apple provides only on first auth)",
    )


class RefreshTokenRequest(BaseModel):
    """Refresh token rotation payload.

    Mirrors the inline request body of the `refreshToken` operation. The token
    travels in this body and nowhere else — there is no cookie channel.

    The field is nonetheless optional so that a missing token is a 400
    (`MISSING_REFRESH_TOKEN`) raised by the route rather than a 422 raised by
    schema validation: an absent body and an empty body must be answered
    identically, and a required field cannot express that.
    """

    refresh_token: str | None = Field(
        default=None,
        description="Opaque refresh token issued by login or a previous rotation; absent or empty is a 400",
    )


class LogoutRequest(BaseModel):
    """Single-device logout payload.

    Mirrors the inline request body of the `logout` operation. The access
    token's `jti` identifies the access token, not the refresh token, so the
    token to revoke must be named explicitly — otherwise the server could only
    revoke every session the user has.

    The token travels in this body and nowhere else — there is no cookie
    channel. The field stays optional because handling is idempotent: an
    unknown token, an already-revoked token, an empty body and no body at all
    all return 204, so the response never discloses whether a token existed.
    A required field would turn the empty-body case into a 422 and break that.
    """

    refresh_token: str | None = Field(
        default=None,
        description="Refresh token to revoke; omitting it succeeds and revokes nothing",
    )


# ── Responses ───────────────────────────────────────────────────────────


class RegisterResponse(BaseModel):
    """Newly created citizen account.

    Mirrors `components.schemas.RegisterResponse`. Note that the contract does
    not return tokens here — the client authenticates via `login` afterwards.
    """

    model_config = ConfigDict(from_attributes=True)

    user_id: UUID = Field(description="Unique identifier of the new account")
    email: str = Field(description="Registered email address", examples=["citizen@example.com"])
    name: str = Field(description="Display name", examples=["Test Citizen"])
    role: UserRole = Field(description="Role assigned to the new account")
    created_at: datetime = Field(description="ISO 8601 account creation timestamp")


class AuthResponse(BaseModel):
    """Token pair plus the authenticated user's profile.

    Mirrors `components.schemas.AuthResponse`. Returned by `login`,
    `googleOAuth` and `appleOAuth`.
    """

    model_config = ConfigDict(from_attributes=True)

    access_token: str = Field(description="RS256-signed JWT access token")
    refresh_token: str = Field(description="Opaque refresh token; send to /auth/refresh to rotate")
    token_type: TokenType = Field(default=TokenType.BEARER, description="Token type; always 'bearer'")
    expires_in: int = Field(description="Access token expiry in seconds", examples=[86400])
    user: UserProfile = Field(description="Profile of the authenticated user")


class TokenResponse(BaseModel):
    """Rotated token pair, without the user profile.

    Mirrors `components.schemas.TokenResponse`. Returned by `refreshToken`.
    Unlike `AuthResponse`, the contract leaves `token_type` as an unconstrained
    string here and omits the `user` object.
    """

    model_config = ConfigDict(from_attributes=True)

    access_token: str = Field(description="RS256-signed JWT access token")
    refresh_token: str = Field(description="Newly issued opaque refresh token; the previous one is revoked")
    token_type: str = Field(default="bearer", description="Token type; always 'bearer'")
    expires_in: int = Field(description="Access token expiry in seconds", examples=[86400])
