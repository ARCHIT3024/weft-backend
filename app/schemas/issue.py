"""Issue lifecycle schemas — submission, listing, detail, triage, upvotes.

Derived from the frozen OpenAPI contract (`openapi.yaml`) component schemas
`CreateIssueRequest`, `CreateIssueResponse`, `IssueSummary`, `IssueDetail`,
`NearbyIssue`, `IssueImage`, `IssueStatusHistoryEntry` and `UpvoteResponse`,
plus the inline request bodies of `updateIssueStatus` and `assignIssue`.

Two deliberate departures from the Phase 0 contract text, both made when the
mock handlers were replaced with real ones (2026-09-08):

* **`ai_suggested_category` / `ai_confidence` are gone.** AI categorisation is
  cut from the MVP; `app/models/issue.py` never grew the columns and
  `tests/unit/test_issue_models.py` pins them as cut. A contract that promises
  two fields the database cannot produce makes every client model them as
  optional-forever, so they were removed from the contract rather than emitted
  as permanent nulls.
* **The response split is `IssueSummary` / `IssueDetail`.** The contract had a
  single `IssueDetail` used for both the list items and the single-issue read.
  A list of 100 issues does not need each one's photo set and full audit trail,
  and `weft-web/src/types/issue.ts` already models the two shapes separately
  (`Issue` and `IssueDetail extends Issue`). The split here matches that file
  field for field.

`reporter_id` appears on no response model. It is stored (nullable, NULL for
anonymous reports) and it drives "my reports", but the public issue read is
world-readable — anonymous is a *product* promise, not just a null column, and
handing every reader the reporter's user id would undo it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

# `issues.description` is TEXT in Postgres; the 500-character ceiling is an
# application rule (05_weft_backend_schema.md §3.7) and lives here.
DESCRIPTION_MAX_LENGTH = 500
NOTE_MAX_LENGTH = 500
ADDRESS_MAX_LENGTH = 500

# Radius ceiling for the spatial filters, per the contract. Beyond a few km the
# flat-earth degree expansion this bounds (see `issue_service`) stops being a
# cheap prefilter, and no civic map view needs more.
MAX_RADIUS_METRES = 10_000
DEFAULT_RADIUS_METRES = 2_000


# ── Enums ───────────────────────────────────────────────────────────────


class IssueCategory(StrEnum):
    """Kind of civic defect being reported.

    Mirrors `components.schemas.IssueCategory` and the `issue_category`
    Postgres enum created in migration 002. All three lists must stay equal.
    """

    POTHOLE = "POTHOLE"
    GARBAGE_ACCUMULATION = "GARBAGE_ACCUMULATION"
    WATER_LOGGING = "WATER_LOGGING"
    BROKEN_STREET_LIGHT = "BROKEN_STREET_LIGHT"
    DAMAGED_FOOTPATH = "DAMAGED_FOOTPATH"
    SEWAGE_OVERFLOW = "SEWAGE_OVERFLOW"
    OTHER = "OTHER"


class IssueStatus(StrEnum):
    """Lifecycle state of an issue.

    Mirrors `components.schemas.IssueStatus` and the `issue_status` Postgres
    enum. Which moves between these are legal is not expressible here — see
    `app.services.issue_service.LEGAL_STATUS_TRANSITIONS`.
    """

    REPORTED = "REPORTED"
    IN_PROGRESS = "IN_PROGRESS"
    RESOLVED = "RESOLVED"
    REJECTED = "REJECTED"


class ImageType(StrEnum):
    """Which side of the lifecycle a photo belongs to."""

    REPORT = "REPORT"
    RESOLUTION_PROOF = "RESOLUTION_PROOF"


class IssueSortField(StrEnum):
    """Columns `GET /issues` may be ordered by.

    Closed on purpose: an open `sort_by` string is an injection surface and an
    invitation to sort by an unindexed column.
    """

    CREATED_AT = "created_at"
    UPVOTE_COUNT = "upvote_count"


# ── Requests ────────────────────────────────────────────────────────────
#
# `POST /issues` takes `multipart/form-data` and is therefore declared as
# `Form(...)`/`File(...)` parameters on the route, not as a model: FastAPI
# 0.111 cannot bind a Pydantic model from a multipart body (that landed in
# 0.113). `components.schemas.CreateIssueRequest` documents that shape, and
# `tests/unit/test_contract_drift.py::test_create_issue_form_matches_contract`
# holds the route signature and the contract together in its place.


class UpdateIssueStatusRequest(BaseModel):
    """Authority triage action: move an issue to a new lifecycle state.

    Mirrors the inline request body of `updateIssueStatus`. `note` is free text
    for the citizen-visible timeline; on a move to `RESOLVED` it is also stored
    as the issue's `resolution_note`.
    """

    status: IssueStatus = Field(description="Target status; the move must be legal from the current one")
    note: str | None = Field(
        default=None,
        max_length=NOTE_MAX_LENGTH,
        description="Optional note recorded on the audit trail entry for this transition",
    )


class AssignIssueRequest(BaseModel):
    """Authority triage action: hand an issue to a named field worker.

    Mirrors the inline request body of `assignIssue`.
    """

    assigned_to_id: uuid.UUID = Field(description="`authority_users.id` of the staff member taking the issue")


# ── Responses ───────────────────────────────────────────────────────────


class IssueImageOut(BaseModel):
    """One photo attached to an issue.

    Mirrors `components.schemas.IssueImage`. `cdn_url` keeps its contract name
    even though the MVP serves images off local disk through the `/media`
    static mount: the field is "where the client fetches this photo", and
    renaming it would break `weft-web/src/types/issue.ts` for a deployment
    detail that is meant to change under it.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(description="Unique identifier of the image row")
    cdn_url: str = Field(description="Client-fetchable URL for the stored image")
    image_type: ImageType = Field(description="Citizen report photo, or authority resolution proof")
    created_at: datetime = Field(description="ISO 8601 upload timestamp")


class IssueStatusHistoryEntry(BaseModel):
    """One transition in an issue's append-only audit trail.

    Mirrors `components.schemas.IssueStatusHistoryEntry`. `previous_status` is
    null for exactly one entry per issue — the `REPORTED` row written at
    submission.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(description="Unique identifier of the history row")
    previous_status: IssueStatus | None = Field(
        default=None,
        description="Status before this transition; null only on the initial REPORTED entry",
    )
    new_status: IssueStatus = Field(description="Status after this transition")
    changed_by_id: uuid.UUID | None = Field(
        default=None,
        description="`users.id` of the actor; null for the submission entry and for deleted accounts",
    )
    note: str | None = Field(default=None, description="Optional note the actor recorded with the transition")
    created_at: datetime = Field(description="ISO 8601 timestamp of the transition")


class IssueSummary(BaseModel):
    """An issue as it appears in a list response.

    Mirrors `components.schemas.IssueSummary`, and `Issue` in
    `weft-web/src/types/issue.ts`. Carries no photos and no audit trail: those
    are per-issue reads, and a 100-item map response should not pay for them.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(description="Unique identifier")
    issue_number: str = Field(
        description="Human-readable reference, unique across all issues",
        examples=["ISS-2026-K7QD3M8XPZ"],
    )
    category: IssueCategory = Field(description="Reported category")
    description: str | None = Field(default=None, description="Citizen's free-text description")
    status: IssueStatus = Field(description="Current lifecycle state")
    latitude: float = Field(ge=-90, le=90, description="WGS84 latitude in degrees")
    longitude: float = Field(ge=-180, le=180, description="WGS84 longitude in degrees")
    address_text: str | None = Field(default=None, description="Reverse-geocoded or citizen-supplied address")
    upvote_count: int = Field(description="Community priority signal, maintained by a database trigger")
    zone_id: uuid.UUID | None = Field(default=None, description="Zone whose boundary contains the report location")
    department_id: uuid.UUID | None = Field(default=None, description="Department the category routed to")
    assigned_to_id: uuid.UUID | None = Field(default=None, description="`authority_users.id` currently responsible")
    resolved_at: datetime | None = Field(default=None, description="ISO 8601 timestamp the issue was resolved")
    created_at: datetime = Field(description="ISO 8601 submission timestamp")
    updated_at: datetime = Field(description="ISO 8601 last-modification timestamp")


class IssueDetail(IssueSummary):
    """A single issue read in full: photos and the complete audit trail.

    Mirrors `components.schemas.IssueDetail`, and `IssueDetail` in
    `weft-web/src/types/issue.ts`.
    """

    assigned_at: datetime | None = Field(default=None, description="ISO 8601 timestamp of the current assignment")
    images: list[IssueImageOut] = Field(description="Report photos and resolution proofs, oldest first")
    status_history: list[IssueStatusHistoryEntry] = Field(description="Every transition, newest first")


class NearbyIssue(IssueSummary):
    """A list item from `GET /issues/nearby`, carrying its distance.

    Mirrors `components.schemas.NearbyIssue`. The distance is computed by the
    same query that selected the row, so it is never re-derived client-side
    from the rounded lat/lng.
    """

    distance_m: float = Field(ge=0, description="Great-circle distance from the query point, in metres")


class CreateIssueResponse(BaseModel):
    """Acknowledgement of a submitted issue.

    Mirrors `components.schemas.CreateIssueResponse`. Deliberately not an
    `IssueDetail`: the client that just posted the form already holds the
    photos and the description, and what it needs back is the identity of the
    thing it created plus the routing decisions the server made.

    `image_url` is the first stored photo, retained under its contract name for
    the single-photo happy path; `images` carries the full set.
    """

    issue_id: uuid.UUID = Field(description="Unique identifier of the created issue")
    issue_number: str = Field(description="Human-readable reference", examples=["ISS-2026-K7QD3M8XPZ"])
    status: IssueStatus = Field(description="Always REPORTED for a freshly submitted issue")
    category: IssueCategory = Field(description="Category as submitted")
    latitude: float = Field(description="WGS84 latitude in degrees")
    longitude: float = Field(description="WGS84 longitude in degrees")
    address_text: str | None = Field(default=None, description="Address as submitted")
    zone_id: uuid.UUID | None = Field(default=None, description="Zone assigned by point-in-polygon lookup")
    department_id: uuid.UUID | None = Field(default=None, description="Department the category routed to")
    image_url: str | None = Field(default=None, description="URL of the first stored photo, if any")
    images: list[IssueImageOut] = Field(description="Every photo stored with this submission")
    created_at: datetime = Field(description="ISO 8601 submission timestamp")


class UpvoteResponse(BaseModel):
    """Result of recording an upvote.

    Mirrors `components.schemas.UpvoteResponse`. `upvote_count` is read back
    from `issues` after the insert, so it is the value the `trg_upvote_count`
    trigger computed — not a number this process incremented.
    """

    issue_id: uuid.UUID = Field(description="Issue that was upvoted")
    user_id: uuid.UUID = Field(description="Citizen who upvoted")
    upvote_count: int = Field(description="Issue's upvote total after this vote")
    created_at: datetime = Field(description="ISO 8601 timestamp the upvote was recorded")
