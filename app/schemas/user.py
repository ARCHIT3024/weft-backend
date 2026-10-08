"""User schemas — profile read/update payloads.

Derived from the frozen OpenAPI contract (`openapi.yaml`) component schemas
`UserProfile` and `UserRole`, plus the inline request body of the
`updateMyProfile` operation (`PATCH /users/me`).

The contract is authoritative for every field name, type and enum value here.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import IDMixin

# ── Enums ───────────────────────────────────────────────────────────────


class UserRole(StrEnum):
    """Role assigned to a user account.

    Mirrors `components.schemas.UserRole` in openapi.yaml. Shared by the auth
    schemas (`RegisterResponse`) and the user schemas (`UserProfile`); defined
    here as its canonical home.
    """

    CITIZEN = "CITIZEN"
    AUTHORITY = "AUTHORITY"
    ADMIN = "ADMIN"


class PreferredLanguage(StrEnum):
    """BCP 47 language codes supported for user-facing content.

    Mirrors the `preferred_lang` enum, which the contract now declares
    identically on both sides of the wire: the `updateMyProfile` request body
    and `components.schemas.UserProfile`. The two lists must stay equal — a
    client that may only *send* one of six values but must *accept* any string
    back has no type it can hold the response in.
    """

    EN = "en"
    HI = "hi"
    TA = "ta"
    TE = "te"
    MR = "mr"
    BN = "bn"


# ── Responses ───────────────────────────────────────────────────────────


class UserProfile(IDMixin):
    """Public profile of a user, including gamification and trust data.

    Mirrors `components.schemas.UserProfile`. Returned by `getMyProfile` and
    embedded in `AuthResponse`. Contains no credential material.
    """

    model_config = ConfigDict(from_attributes=True)

    email: str | None = Field(
        default=None,
        description="Account email; null for anonymous or phone-only accounts",
        examples=["citizen@example.com"],
    )
    name: str | None = Field(
        default=None,
        description="Display name; null for anonymous accounts",
        examples=["Test Citizen"],
    )
    role: UserRole = Field(description="Role assigned to this account")
    trust_score: float = Field(
        ge=0,
        le=100,
        description="Percentage of this user's reports that were resolved (0-100)",
        examples=[75.5],
    )
    total_points: int = Field(
        description="Cumulative gamification points; may be negative after spam penalties",
        examples=[120],
    )
    title: str | None = Field(
        default=None,
        description="Gamification title earned from total_points (Newcomer, Helper, "
        "Civic Contributor, City Guardian, City Hero)",
        examples=["Civic Contributor"],
    )
    is_anonymous: bool = Field(description="True for anonymous accounts that store no PII")
    preferred_lang: PreferredLanguage = Field(
        description="Preferred language as a BCP 47 code (en, hi, ta, te, mr, bn)",
        examples=[PreferredLanguage.EN],
    )
    created_at: datetime = Field(description="ISO 8601 account creation timestamp")


# ── Requests ────────────────────────────────────────────────────────────


class UpdateProfileRequest(BaseModel):
    """Partial update of the caller's own profile.

    Mirrors the inline request body of `updateMyProfile`. Every field is
    optional; omitted fields are left unchanged.
    """

    name: str | None = Field(
        default=None,
        max_length=100,
        description="New display name",
        examples=["Updated Name"],
    )
    preferred_lang: PreferredLanguage | None = Field(
        default=None,
        description="New preferred language",
        examples=[PreferredLanguage.HI],
    )
    fcm_token: str | None = Field(
        default=None,
        description="Firebase Cloud Messaging device token for push notifications",
    )
