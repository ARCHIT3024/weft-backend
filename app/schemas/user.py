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

# FCM registration tokens are ~160 characters today. The ceiling is generous
# headroom for a format change, not a guess at the real length — it exists so a
# client cannot park megabytes in a TEXT column through a profile update.
FCM_TOKEN_MAX_LENGTH = 4096

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
    optional; an omitted field — or one sent as `null` — is left unchanged.
    Clearing a field is not an operation this endpoint offers.

    **Unknown fields are rejected (422), not ignored.** The field is
    `fcm_token`; implementation-plan task 2.31 calls it "device_token" in
    prose. A client that sent `device_token` to a lenient model would get a
    200 and never receive a push, with nothing anywhere to say why. Rejecting
    the unknown key turns that silent failure into one the client developer
    sees on the first call.

    `phone` and `email` are not editable here: both are login identifiers and
    unique, and changing one without verifying the new value would let an
    account claim an address it does not control.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        description="New display name; surrounding whitespace is stripped and a blank name is rejected",
        examples=["Updated Name"],
    )
    preferred_lang: PreferredLanguage | None = Field(
        default=None,
        description="New preferred language",
        examples=[PreferredLanguage.HI],
    )
    fcm_token: str | None = Field(
        default=None,
        min_length=1,
        max_length=FCM_TOKEN_MAX_LENGTH,
        description="Firebase Cloud Messaging registration token for this device. Send it on every app "
        "launch; it registers the device for push and moves it to the caller if another account had it.",
    )
