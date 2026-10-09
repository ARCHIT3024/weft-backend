"""Issues router — the citizen submission and authority triage surface.

Every handler is a translation layer: parse the request, delegate to
`app.services.issue_service`, shape the response. No query and no business rule
lives here.

`POST /issues` is `multipart/form-data`, so its fields are declared as
`Form(...)`/`File(...)` parameters rather than a Pydantic body model — FastAPI
0.111 cannot bind a model from a multipart body (that arrived in 0.113).
`components.schemas.CreateIssueRequest` in `openapi.yaml` documents the shape.

`POST /issues/{id}/images` is still a Phase 0 mock: authority resolution-proof
upload is task 1.23 and needs the authority-side flow that does not exist yet.
It is left in place so the contract surface does not shrink.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Query, UploadFile, status

from app.config import settings
from app.core.captcha import verify_captcha
from app.core.exceptions import BadRequestError, ForbiddenError
from app.core.permissions import Role, get_current_user, get_current_user_optional, require_role
from app.dependencies import DBSession, RedisClient, enforce_issue_rate_limit
from app.models.user import User
from app.schemas.common import PaginatedResponse
from app.schemas.issue import (
    DEFAULT_RADIUS_METRES,
    MAX_RADIUS_METRES,
    AssignableStaff,
    AssignableStaffList,
    AssignIssueRequest,
    CreateIssueResponse,
    IssueCategory,
    IssueDetail,
    IssueImageOut,
    IssueStatus,
    IssueStatusHistoryEntry,
    IssueSummary,
    NearbyIssue,
    UpdateIssueStatusRequest,
)
from app.services import image_service, issue_service, notification_service, realtime_service

logger = logging.getLogger(__name__)

router = APIRouter()

CurrentUser = Annotated[User, Depends(get_current_user)]
OptionalUser = Annotated[User | None, Depends(get_current_user_optional)]
AuthorityUserDep = Annotated[User, Depends(require_role(Role.AUTHORITY, Role.ADMIN))]


def _image_url(file_path: str) -> str:
    """Storage key → client-fetchable URL."""
    return f"{settings.MEDIA_BASE_URL.rstrip('/')}/{file_path.lstrip('/')}"


def _to_image_out(image) -> IssueImageOut:  # noqa: ANN001 — IssueImage ORM row
    return IssueImageOut(
        id=image.id,
        cdn_url=_image_url(image.file_path),
        image_type=image.image_type,
        created_at=image.created_at,
    )


def _to_summary(issue) -> IssueSummary:  # noqa: ANN001 — Issue ORM row
    return IssueSummary(
        id=issue.id,
        issue_number=issue.issue_number,
        category=issue.category,
        description=issue.description,
        status=issue.status,
        latitude=float(issue.latitude),
        longitude=float(issue.longitude),
        address_text=issue.address_text,
        upvote_count=issue.upvote_count,
        zone_id=issue.zone_id,
        department_id=issue.department_id,
        assigned_to_id=issue.assigned_to_id,
        resolved_at=issue.resolved_at,
        created_at=issue.created_at,
        updated_at=issue.updated_at,
    )


# ── POST /issues ────────────────────────────────────────────────────────


@router.post(
    "",
    response_model=CreateIssueResponse,
    status_code=status.HTTP_201_CREATED,
    # Route-level, so a refused submission never reaches CAPTCHA, image
    # decoding or the insert. 10/hour/user, 3/hour/IP anonymous, per TRD
    # Section 6; fails open when Redis is down — see the dependency.
    dependencies=[Depends(enforce_issue_rate_limit)],
    summary="Submit a civic issue",
)
async def create_issue(
    db: DBSession,
    current_user: OptionalUser,
    background_tasks: BackgroundTasks,
    redis: RedisClient,
    category: Annotated[IssueCategory, Form(description="Reported category")],
    latitude: Annotated[float, Form(ge=-90, le=90)],
    longitude: Annotated[float, Form(ge=-180, le=180)],
    description: Annotated[str | None, Form(max_length=500)] = None,
    address_text: Annotated[str | None, Form(max_length=500)] = None,
    captcha_token: Annotated[str | None, Form()] = None,
    # `list[UploadFile]` with a plain default, NOT `list[UploadFile] | None`.
    # Under the union, FastAPI does not collect the repeated multipart field
    # into a list, and a single uploaded file fails validation with
    # "Input should be a valid list". An empty list is the no-images case.
    images: Annotated[list[UploadFile], File()] = [],  # noqa: B006 — FastAPI reads, never mutates
) -> CreateIssueResponse:
    """Create an issue. **Authentication is optional** — anonymous reports are a
    product requirement, so `reporter_id` is simply NULL for them.

    CAPTCHA is verified for anonymous submissions **only when a secret key is
    configured**. In development `RECAPTCHA_SECRET_KEY` is empty, and a missing
    dev key must never be the thing that blocks a submission.

    Image failures do not roll back the report. A civic hazard that was reported
    with an unreadable photo is still a reported hazard, and discarding it would
    be the worse outcome; rejected files are counted in the response instead.

    `issue.created` goes to the dashboard feed as a background task, so it is
    published only once the submission has committed, and a Redis outage can
    never fail the submission (see `app/core/events.py`).
    """
    files = [f for f in images if f is not None and f.filename]
    if len(files) > settings.MAX_IMAGES_PER_ISSUE:
        raise BadRequestError(
            code="TOO_MANY_IMAGES",
            message=f"At most {settings.MAX_IMAGES_PER_ISSUE} images may be attached.",
            details={"max_images": settings.MAX_IMAGES_PER_ISSUE, "received": len(files)},
        )

    if current_user is None and settings.RECAPTCHA_SECRET_KEY:
        if not captcha_token:
            raise BadRequestError(code="CAPTCHA_REQUIRED", message="captcha_token is required.")
        await verify_captcha(captcha_token)
    elif current_user is None:
        logger.debug("CAPTCHA skipped: RECAPTCHA_SECRET_KEY is not configured")

    issue = await issue_service.create_issue(
        db,
        category=category.value,
        latitude=latitude,
        longitude=longitude,
        description=description,
        address_text=address_text,
        reporter=current_user,
    )

    stored: list[IssueImageOut] = []
    for upload in files:
        raw = await upload.read()
        try:
            result = await image_service.store_issue_image(
                issue_id=issue.id,
                filename=upload.filename or "upload",
                raw=raw,
            )
        except Exception:
            # Deliberately broad and deliberately non-fatal: see the docstring.
            logger.warning("Rejected image on issue_id=%s filename=%s", issue.id, upload.filename)
            continue
        row = await issue_service.attach_image(db, issue_id=issue.id, file_path=result.file_path)
        stored.append(_to_image_out(row))

    realtime_service.schedule_issue_created(background_tasks, redis, issue=_to_summary(issue))
    return CreateIssueResponse(
        issue_id=issue.id,
        issue_number=issue.issue_number,
        status=issue.status,
        category=issue.category,
        latitude=float(issue.latitude),
        longitude=float(issue.longitude),
        address_text=issue.address_text,
        zone_id=issue.zone_id,
        department_id=issue.department_id,
        image_url=stored[0].cdn_url if stored else None,
        images=stored,
        created_at=issue.created_at,
    )


# ── GET /issues ─────────────────────────────────────────────────────────


@router.get("", response_model=PaginatedResponse[IssueSummary], summary="List issues")
async def list_issues(
    db: DBSession,
    category: Annotated[list[IssueCategory] | None, Query()] = None,
    issue_status: Annotated[list[IssueStatus] | None, Query(alias="status")] = None,
    zone_id: uuid.UUID | None = None,
    min_upvotes: Annotated[int | None, Query(ge=0)] = None,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    lat: Annotated[float | None, Query(ge=-90, le=90)] = None,
    lng: Annotated[float | None, Query(ge=-180, le=180)] = None,
    radius: Annotated[int | None, Query(ge=1, le=MAX_RADIUS_METRES)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    sort: str = "created_at",
    order: str = "desc",
) -> PaginatedResponse[IssueSummary]:
    """Paginated, filterable issue list — the dashboard's main query."""
    filters = issue_service.IssueFilters(
        categories=[c.value for c in category] if category else None,
        statuses=[s.value for s in issue_status] if issue_status else None,
        zone_id=zone_id,
        min_upvotes=min_upvotes,
        from_date=from_date,
        to_date=to_date,
        latitude=lat,
        longitude=lng,
        radius_m=float(radius) if radius else None,
    )
    rows, total = await issue_service.list_issues(
        db,
        filters=filters,
        page=page,
        page_size=page_size,
        sort_field=sort,
        descending=order.lower() != "asc",
    )
    return PaginatedResponse.create(
        items=[_to_summary(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


# ── GET /issues/nearby ──────────────────────────────────────────────────
#
# Declared before `/{issue_id}` on purpose: FastAPI matches routes in
# declaration order, so the parameterised route would otherwise swallow
# "nearby" and try to parse it as a UUID.


@router.get("/nearby", response_model=list[NearbyIssue], summary="Issues near a point")
async def nearby_issues(
    db: DBSession,
    lat: Annotated[float, Query(ge=-90, le=90)],
    lng: Annotated[float, Query(ge=-180, le=180)],
    radius: Annotated[int, Query(ge=1, le=MAX_RADIUS_METRES)] = DEFAULT_RADIUS_METRES,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[NearbyIssue]:
    """Issues within `radius` metres, nearest first, with real distances."""
    results = await issue_service.find_nearby(db, latitude=lat, longitude=lng, radius_m=float(radius), limit=limit)
    return [NearbyIssue(**_to_summary(issue).model_dump(), distance_m=distance) for issue, distance in results]


# ── GET /issues/{id} ────────────────────────────────────────────────────


@router.get("/{issue_id}", response_model=IssueDetail, summary="Issue detail")
async def get_issue(db: DBSession, issue_id: uuid.UUID) -> IssueDetail:
    """Full record: images and the complete status history."""
    issue = await issue_service.get_issue(db, issue_id)
    history = await issue_service.get_status_history(db, issue_id)

    return IssueDetail(
        **_to_summary(issue).model_dump(),
        assigned_at=issue.assigned_at,
        images=[_to_image_out(i) for i in issue.images],
        status_history=[
            IssueStatusHistoryEntry(
                id=h.id,
                previous_status=h.previous_status,
                new_status=h.new_status,
                changed_by_id=h.changed_by_id,
                note=h.note,
                created_at=h.created_at,
            )
            for h in history
        ],
    )


# ── PATCH /issues/{id}/status ───────────────────────────────────────────


@router.patch("/{issue_id}/status", response_model=IssueDetail, summary="Change issue status")
async def update_issue_status(
    db: DBSession,
    issue_id: uuid.UUID,
    payload: UpdateIssueStatusRequest,
    current_user: AuthorityUserDep,
    background_tasks: BackgroundTasks,
    redis: RedisClient,
) -> IssueDetail:
    """Authority triage. Illegal transitions are refused, not silently applied.

    The reporter's notification (task 1.21/1.25) is queued as a background
    task: it runs after the response, after the request session has committed,
    and can never fail the status change. Anonymous issues notify nobody. The
    dashboard's `issue.status_changed` event is queued the same way.
    """
    issue = await issue_service.update_status(
        db,
        issue_id=issue_id,
        new_status=payload.status.value,
        actor=current_user,
        note=payload.note,
    )
    notification_service.schedule_status_change_notification(background_tasks, db, issue=issue, actor=current_user)
    detail = await get_issue(db, issue_id)
    realtime_service.schedule_status_changed(
        background_tasks,
        redis,
        issue=detail,
        # History is newest first, so this is the row the transition just wrote.
        previous_status=detail.status_history[0].previous_status if detail.status_history else None,
        actor=current_user,
        note=payload.note,
    )
    return detail


# ── PATCH /issues/{id}/assign ───────────────────────────────────────────


@router.patch("/{issue_id}/assign", response_model=IssueDetail, summary="Assign an issue")
async def assign_issue(
    db: DBSession,
    issue_id: uuid.UUID,
    payload: AssignIssueRequest,
    current_user: AuthorityUserDep,
    background_tasks: BackgroundTasks,
    redis: RedisClient,
) -> IssueDetail:
    """Assign to an authority staff member.

    `issue.assigned` is published to the dashboard feed after commit.
    """
    await issue_service.assign_issue(
        db,
        issue_id=issue_id,
        assigned_to_id=payload.assigned_to_id,
        actor=current_user,
    )
    detail = await get_issue(db, issue_id)
    realtime_service.schedule_assigned(
        background_tasks, redis, issue=detail, assigned_at=detail.assigned_at, actor=current_user
    )
    return detail


# ── GET /issues/{id}/assignable-staff ───────────────────────────────────


@router.get(
    "/{issue_id}/assignable-staff",
    response_model=AssignableStaffList,
    summary="Staff this issue can be assigned to",
)
async def list_assignable_staff(
    db: DBSession,
    issue_id: uuid.UUID,
    current_user: AuthorityUserDep,
) -> AssignableStaffList:
    """The assign control's picker: exactly the staff `PATCH /assign` would accept.

    Same jurisdiction rule as the triage writes — an issue outside the caller's
    zones is a 404, identical to a missing one.
    """
    rows = await issue_service.list_assignable_staff(db, issue_id=issue_id, actor=current_user)
    return AssignableStaffList(items=[AssignableStaff.model_validate(row) for row in rows])


# ── POST /issues/{id}/images  (still a Phase 0 mock) ────────────────────


@router.post("/{issue_id}/images", status_code=status.HTTP_201_CREATED)
async def upload_resolution_proof_mock(issue_id: str, current_user: AuthorityUserDep) -> dict:
    """Mock: authority resolution-proof upload (task 1.23, not implemented).

    Kept as a stub so the contract surface does not shrink, but now behind the
    authority guard — an unauthenticated write endpoint, even a mock one, is
    not something to leave reachable.
    """
    if not issue_id:
        raise ForbiddenError("Issue id required")
    return {
        "id": "550e8400-e29b-41d4-a716-446655440010",
        "issue_id": issue_id,
        "cdn_url": "https://cdn.weft.city/mock/resolution-proof.jpg",
        "image_type": "RESOLUTION_PROOF",
        "created_at": "2026-07-02T12:00:00Z",
    }
