"""Guard the Pydantic schemas against drift from the OpenAPI contract.

`openapi.yaml` is the frozen, signed-off API contract (Phase 0 task 0.5) and is
authoritative for every field name, enum value, and required flag. The schemas in
`app/schemas/` are hand-derived from it, so nothing but a test keeps the two in
step: a field renamed on one side and not the other stays green until a client
breaks against staging.

These checks compare the two mechanically rather than by eye. They need no
database and no network, so they run in the unit tier on every push.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from app.schemas.admin import (
    AuthorityUserOut,
    CreateAuthorityRequest,
    CreateDepartmentRequest,
    CreateZoneRequest,
    DepartmentListResponse,
    DepartmentOut,
    GeoJSONPolygon,
    SystemStats,
    UpdateDepartmentRequest,
    ZoneListResponse,
    ZoneOut,
)
from app.schemas.analytics import (
    AnalyticsSummary,
    CategoryCount,
    ExportFormat,
    HeatmapPoint,
    HeatmapResponse,
    ResolutionGroupBy,
    ResolutionTimeGroup,
    ResolutionTimesResponse,
    ResolutionTimeStats,
    ResolutionTrendPoint,
    SlaBreach,
    TrendInterval,
)
from app.schemas.auth import (
    AppleOAuthRequest,
    AuthResponse,
    GoogleOAuthRequest,
    LoginRequest,
    LogoutRequest,
    RefreshTokenRequest,
    RegisterRequest,
    RegisterResponse,
    TokenResponse,
    TokenType,
)
from app.schemas.common import ErrorDetail, ErrorResponse, HealthResponse, PaginatedResponse
from app.schemas.department import DepartmentSummary, DepartmentSummaryList
from app.schemas.issue import (
    AssignableStaff,
    AssignableStaffList,
    AssignIssueRequest,
    CreateIssueResponse,
    IssueDetail,
    IssueImageOut,
    IssueStatus,
    IssueStatusHistoryEntry,
    IssueSummary,
    NearbyIssue,
    UpdateIssueStatusRequest,
    UpvoteResponse,
)
from app.schemas.notification import (
    MarkAllNotificationsReadResponse,
    NotificationChannel,
    NotificationListResponse,
    NotificationOut,
    NotificationType,
)
from app.schemas.user import PreferredLanguage, UpdateProfileRequest, UserProfile, UserRole

# Resolved from this file, not the process CWD, so the suite passes regardless of
# where pytest is invoked from.
OPENAPI_PATH = Path(__file__).resolve().parents[2] / "openapi.yaml"

OPENAPI_TEXT: str = OPENAPI_PATH.read_text(encoding="utf-8")
_spec: dict[str, Any] = yaml.safe_load(OPENAPI_TEXT)
COMPONENTS: dict[str, Any] = _spec["components"]["schemas"]
PATHS: dict[str, Any] = _spec["paths"]

HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options", "trace"})


def _operations() -> list[tuple[str, str, dict[str, Any]]]:
    """Every (path, method, operation) triple in the contract."""
    return [
        (path, method, operation)
        for path, item in PATHS.items()
        for method, operation in item.items()
        if method in HTTP_METHODS
    ]


def _inline_body(path: str, method: str) -> dict[str, Any]:
    """Return the JSON request-body schema for an operation with an inline body."""
    return PATHS[path][method]["requestBody"]["content"]["application/json"]["schema"]


def _enum_members(annotation: Any) -> list[str] | None:
    """Extract enum values from a field annotation, unwrapping optionals."""
    for candidate in (annotation, *getattr(annotation, "__args__", ())):
        if isinstance(candidate, type) and issubclass(candidate, str) and hasattr(candidate, "__members__"):
            return [member.value for member in candidate]
    return None


# ── Component schemas ───────────────────────────────────────────────────

COMPONENT_CASES = [
    (RegisterRequest, "RegisterRequest"),
    (RegisterResponse, "RegisterResponse"),
    (LoginRequest, "LoginRequest"),
    (AuthResponse, "AuthResponse"),
    (TokenResponse, "TokenResponse"),
    (UserProfile, "UserProfile"),
    (ErrorResponse, "ErrorResponse"),
    (HealthResponse, "HealthResponse"),
    # Admin (tasks 1.11, 1.12, 4.15a)
    (CreateAuthorityRequest, "CreateAuthorityRequest"),
    (AuthorityUserOut, "AuthorityUser"),
    (PaginatedResponse, "PaginatedAuthorityUserResponse"),
    (DepartmentOut, "Department"),
    (DepartmentListResponse, "DepartmentList"),
    (CreateDepartmentRequest, "CreateDepartmentRequest"),
    (UpdateDepartmentRequest, "UpdateDepartmentRequest"),
    (GeoJSONPolygon, "GeoJSONPolygon"),
    (CreateZoneRequest, "CreateZoneRequest"),
    (ZoneOut, "Zone"),
    (ZoneListResponse, "ZoneList"),
    (SystemStats, "SystemStats"),
    # Users + notifications (tasks 1.10, 1.26, 2.28)
    (IssueSummary, "IssueSummary"),
    (PaginatedResponse, "PaginatedIssueSummaryResponse"),
    (NotificationOut, "Notification"),
    (NotificationListResponse, "NotificationListResponse"),
    (MarkAllNotificationsReadResponse, "MarkAllNotificationsReadResponse"),
    # Analytics (task 1.29, SLA breaches, CSV export)
    (AnalyticsSummary, "AnalyticsSummary"),
    (CategoryCount, "CategoryCount"),
    (HeatmapResponse, "HeatmapResponse"),
    (HeatmapPoint, "HeatmapPoint"),
    (ResolutionTimeStats, "ResolutionTimeStats"),
    (ResolutionTimeGroup, "ResolutionTimeGroup"),
    (ResolutionTrendPoint, "ResolutionTrendPoint"),
    (ResolutionTimesResponse, "ResolutionTimesResponse"),
    (SlaBreach, "SlaBreach"),
    (PaginatedResponse, "PaginatedSlaBreachResponse"),
    # Issues — real handlers since 2026-09-09; required lists added with the
    # staff-scope work (they were withheld while these backed mocks).
    (CreateIssueResponse, "CreateIssueResponse"),
    (IssueDetail, "IssueDetail"),
    (IssueImageOut, "IssueImage"),
    (IssueStatusHistoryEntry, "IssueStatusHistoryEntry"),
    (NearbyIssue, "NearbyIssue"),
    (UpvoteResponse, "UpvoteResponse"),
    (AssignableStaff, "AssignableStaff"),
    (AssignableStaffList, "AssignableStaffList"),
    # Staff department read
    (DepartmentSummary, "DepartmentSummary"),
    (DepartmentSummaryList, "DepartmentSummaryList"),
]


@pytest.mark.parametrize(("model", "component"), COMPONENT_CASES, ids=[c for _, c in COMPONENT_CASES])
def test_component_schema_fields_match_contract(model: type, component: str) -> None:
    contract = set(COMPONENTS[component].get("properties", {}))
    declared = set(model.model_fields)
    assert (
        declared == contract
    ), f"{component}: missing={sorted(contract - declared)} extra={sorted(declared - contract)}"


@pytest.mark.parametrize(("model", "component"), COMPONENT_CASES, ids=[c for _, c in COMPONENT_CASES])
def test_component_schema_required_matches_contract(model: type, component: str) -> None:
    """Every component backed by a model must declare the same `required` list it does.

    A component schema with no `required` array makes client codegen emit an
    all-optional model, so both frontends lose the compile-time guarantee that
    e.g. `TokenResponse.refresh_token` is always there — and a client that
    treats the rotated refresh token as optional silently ends its own session.

    The rule is mechanical: required means "no default", which is exactly what
    the app's own generated `/openapi.json` reports, so the frozen contract and
    the live schema cannot disagree.
    """
    contract_required = set(COMPONENTS[component].get("required", []))
    declared_required = {name for name, f in model.model_fields.items() if f.is_required()}
    assert declared_required == contract_required, (
        f"{component}: missing={sorted(declared_required - contract_required)} "
        f"extra={sorted(contract_required - declared_required)}"
    )


def test_error_detail_required_matches_contract() -> None:
    """`ErrorResponse.error` is inlined in the contract, so it needs its own check."""
    contract = COMPONENTS["ErrorResponse"]["properties"]["error"]
    assert set(contract["properties"]) == set(ErrorDetail.model_fields)
    declared_required = {name for name, f in ErrorDetail.model_fields.items() if f.is_required()}
    assert set(contract["required"]) == declared_required


def test_every_required_entry_names_a_real_property() -> None:
    """A typo in a `required` list is otherwise invisible until codegen runs.

    Sweeps every component, including the ones with no Pydantic counterpart.
    """
    for name, schema in COMPONENTS.items():
        if not isinstance(schema, dict):
            continue
        unknown = set(schema.get("required", [])) - set(schema.get("properties", {}))
        assert not unknown, f"{name}: required names undeclared propert(ies) {sorted(unknown)}"


# ── Inline request bodies ───────────────────────────────────────────────

INLINE_CASES = [
    (GoogleOAuthRequest, "/auth/oauth/google", "post"),
    (AppleOAuthRequest, "/auth/oauth/apple", "post"),
    (RefreshTokenRequest, "/auth/refresh", "post"),
    (UpdateProfileRequest, "/users/me", "patch"),
    (LogoutRequest, "/auth/logout", "post"),
    (UpdateIssueStatusRequest, "/issues/{issue_id}/status", "patch"),
    (AssignIssueRequest, "/issues/{issue_id}/assign", "patch"),
]


@pytest.mark.parametrize(("model", "path", "method"), INLINE_CASES, ids=[p for _, p, _ in INLINE_CASES])
def test_inline_request_body_fields_match_contract(model: type, path: str, method: str) -> None:
    contract = set(_inline_body(path, method).get("properties", {}))
    declared = set(model.model_fields)
    assert (
        declared == contract
    ), f"{path} {method}: missing={sorted(contract - declared)} extra={sorted(declared - contract)}"


@pytest.mark.parametrize(("model", "path", "method"), INLINE_CASES, ids=[p for _, p, _ in INLINE_CASES])
def test_inline_request_body_required_matches_contract(model: type, path: str, method: str) -> None:
    contract_required = set(_inline_body(path, method).get("required", []))
    declared_required = {name for name, f in model.model_fields.items() if f.is_required()}
    assert declared_required == contract_required


# ── Enums ───────────────────────────────────────────────────────────────


def test_user_role_enum_matches_contract() -> None:
    assert [m.value for m in UserRole] == COMPONENTS["UserRole"]["enum"]


def test_token_type_enum_matches_contract() -> None:
    assert [m.value for m in TokenType] == COMPONENTS["AuthResponse"]["properties"]["token_type"]["enum"]


def test_preferred_language_enum_matches_contract() -> None:
    contract = _inline_body("/users/me", "patch")["properties"]["preferred_lang"]["enum"]
    assert [m.value for m in PreferredLanguage] == contract


def test_preferred_lang_is_enum_constrained_on_output_too() -> None:
    """`UserProfile.preferred_lang` must be the enum, not a bare string.

    Asymmetry here is a real client defect, not cosmetics: a client that may
    only *send* one of six languages but must *accept* any string back has no
    single type it can hold the value in, and every read site needs a widening
    cast or a runtime check the server can already guarantee.
    """
    assert COMPONENTS["UserProfile"]["properties"]["preferred_lang"].get("enum") == [m.value for m in PreferredLanguage]


def test_preferred_lang_enum_is_identical_on_both_sides_of_the_wire() -> None:
    """The input enum and the output enum are duplicated inline; keep them equal."""
    request_enum = _inline_body("/users/me", "patch")["properties"]["preferred_lang"]["enum"]
    response_enum = COMPONENTS["UserProfile"]["properties"]["preferred_lang"]["enum"]
    assert request_enum == response_enum


@pytest.mark.parametrize(("model", "component"), COMPONENT_CASES, ids=[c for _, c in COMPONENT_CASES])
def test_enum_constrained_fields_use_an_enum_type(model: type, component: str) -> None:
    """Any field the contract constrains to an enum must be modelled as one.

    A contract enum modelled as a plain `str` silently accepts values the API
    rejects, so the mismatch surfaces as a 422 from staging rather than at the
    boundary.
    """
    for name, prop in COMPONENTS[component].get("properties", {}).items():
        expected = prop.get("enum")
        if ref := prop.get("$ref"):
            expected = COMPONENTS[ref.split("/")[-1]].get("enum")
        if not expected:
            continue
        members = _enum_members(model.model_fields[name].annotation)
        assert (
            members is not None
        ), f"{component}.{name} is enum-constrained in the contract but modelled as a plain type"
        assert sorted(members) == sorted(expected), f"{component}.{name}: contract={expected} model={members}"


# ── Refresh tokens travel in the JSON body, and only there ──────────────

BODY_TOKEN_OPERATIONS = [("/auth/logout", LogoutRequest), ("/auth/refresh", RefreshTokenRequest)]


@pytest.mark.parametrize(("path", "model"), BODY_TOKEN_OPERATIONS, ids=[p for p, _ in BODY_TOKEN_OPERATIONS])
def test_refresh_token_is_taken_from_the_json_body(path: str, model: type) -> None:
    """These operations read the token from the JSON body and from nowhere else.

    The access token's `jti` identifies the access token, not the refresh token,
    so the token to act on cannot be inferred and must be named explicitly. The
    body is the only channel: the MVP pivot dropped the `weft_refresh` cookie,
    and neither route reads a cookie or sets one.

    `refresh_token` stays optional in both the contract and the model so that an
    absent body and an empty body are answered identically — a 400 for refresh,
    a 204 for logout. A `required` list would make the absent-body case a 422
    and split those paths apart.
    """
    assert PATHS[path]["post"]["requestBody"]["required"] is False
    body = _inline_body(path, "post")
    assert "required" not in body, f"{path}: a required list would turn the empty-body case into a 422"
    # Logout also takes an optional `fcm_token`; the exact field sets are pinned
    # by the INLINE_CASES tests. What matters here is that nothing is required.
    assert "refresh_token" in model.model_fields
    assert not any(field.is_required() for field in model.model_fields.values())


NO_COOKIE_VOCABULARY = ["weft_refresh", "Set-Cookie", "HttpOnly", "httpOnly", "SameSite"]


@pytest.mark.parametrize("term", NO_COOKIE_VOCABULARY)
def test_contract_describes_no_refresh_cookie(term: str) -> None:
    """No cookie is read or emitted anywhere, so no cookie may be documented.

    The contract described an httpOnly `weft_refresh` cookie with `Set-Cookie`
    response headers on every token-issuing operation. The implementation never
    had one. A frontend built to that text waits for a cookie that never
    arrives and has no session at all.
    """
    assert term not in OPENAPI_TEXT, f"contract still mentions {term!r}"


def test_no_operation_documents_a_set_cookie_response_header() -> None:
    """Structural counterpart to the text sweep above."""
    offenders = [
        f"{method.upper()} {path} -> {code}"
        for path, method, operation in _operations()
        for code, response in operation.get("responses", {}).items()
        if "Set-Cookie" in (response.get("headers") or {})
    ]
    assert not offenders, f"operations documenting a Set-Cookie header: {offenders}"


# ── Logout is unauthenticated and idempotent ────────────────────────────


def test_logout_requires_no_bearer_token() -> None:
    """Logout must stay reachable without an access token.

    Possession of the 64-byte refresh token is the proof. Requiring a live
    bearer token would make an expired session impossible to revoke — precisely
    the case where revocation matters — so the operation declares no security
    requirement, and the contract declares no global default that would
    reinstate one.
    """
    assert "security" not in _spec, "a global security requirement would re-apply BearerAuth to logout"
    assert "security" not in PATHS["/auth/logout"]["post"]


def test_logout_documents_only_the_idempotent_204() -> None:
    """Unknown, already-revoked, empty and absent tokens all return 204.

    Any documented 4xx here would make the response an oracle for which tokens
    exist; a 404 on an unknown token is exactly the leak D-3 rejected.
    """
    assert set(PATHS["/auth/logout"]["post"]["responses"]) == {"204"}


def test_logout_204_carries_no_response_headers() -> None:
    assert "headers" not in PATHS["/auth/logout"]["post"]["responses"]["204"]


# ── Refresh always returns both tokens ──────────────────────────────────


def test_refresh_returns_both_tokens() -> None:
    """The rotated refresh token is not optional in the response.

    Rotation revokes the presented token in the same transaction that issues its
    replacement. A client that skipped the new refresh_token would be holding a
    dead credential and lose the session on the next rotation, so the contract
    must mark both tokens required.
    """
    ok = PATHS["/auth/refresh"]["post"]["responses"]["200"]
    assert ok["content"]["application/json"]["schema"]["$ref"] == "#/components/schemas/TokenResponse"
    assert {"access_token", "refresh_token"} <= set(COMPONENTS["TokenResponse"]["required"])


def test_refresh_documents_the_missing_token_bad_request() -> None:
    assert "400" in PATHS["/auth/refresh"]["post"]["responses"]
    assert "401" in PATHS["/auth/refresh"]["post"]["responses"]


# ── GET /auth/me is not part of the contract ────────────────────────────


def test_auth_me_is_absent_from_the_contract() -> None:
    """`GET /users/me` is the canonical profile endpoint and the implemented one.

    `/auth/me` was never contracted. It existed only as a Phase 0 mock handler,
    deleted 2026-09-08 under D-7. Pinned so a future sweep cannot add it back by
    symmetry with `/auth/login`.
    """
    assert "/auth/me" not in PATHS


# ── Credential material must never appear in a response ─────────────────

RESPONSE_MODELS = [RegisterResponse, AuthResponse, TokenResponse, UserProfile, AuthorityUserOut]


@pytest.mark.parametrize("model", RESPONSE_MODELS, ids=[m.__name__ for m in RESPONSE_MODELS])
def test_response_models_expose_no_credential_material(model: type) -> None:
    """Refresh tokens are issued in responses by design; passwords never are."""
    leaked = [name for name in model.model_fields if "password" in name.lower() or name.endswith("_hash")]
    assert not leaked, f"{model.__name__} exposes credential field(s): {leaked}"


# ── Users + notifications ───────────────────────────────────────────────


def test_notification_type_enum_matches_contract_and_migration_002() -> None:
    assert [m.value for m in NotificationType] == COMPONENTS["NotificationType"]["enum"]


def test_notification_channel_enum_matches_contract() -> None:
    assert [m.value for m in NotificationChannel] == COMPONENTS["NotificationChannel"]["enum"]


def test_my_reports_status_filter_uses_the_issue_status_enum() -> None:
    params = {p["name"]: p for p in PATHS["/users/me/reports"]["get"]["parameters"]}
    assert params["status"]["schema"]["items"]["$ref"] == "#/components/schemas/IssueStatus"
    assert COMPONENTS["IssueStatus"]["enum"] == [m.value for m in IssueStatus]


@pytest.mark.parametrize(
    ("path", "method", "component"),
    [
        ("/users/me", "patch", "UserProfile"),
        ("/users/me/reports", "get", "PaginatedIssueSummaryResponse"),
        ("/notifications", "get", "NotificationListResponse"),
        ("/notifications/{notification_id}/read", "patch", "Notification"),
        ("/notifications/read-all", "patch", "MarkAllNotificationsReadResponse"),
    ],
)
def test_real_user_and_notification_endpoints_document_their_response(path: str, method: str, component: str) -> None:
    """These were Phase 0 mocks with undocumented bodies; each now names its model."""
    ok = PATHS[path][method]["responses"]["200"]
    assert ok["content"]["application/json"]["schema"]["$ref"] == f"#/components/schemas/{component}"
    assert PATHS[path][method]["security"] == [{"BearerAuth": []}]
    assert "401" in PATHS[path][method]["responses"]


def test_mark_read_documents_404_and_not_403() -> None:
    """Another user's notification is indistinguishable from a missing one."""
    responses = PATHS["/notifications/{notification_id}/read"]["patch"]["responses"]
    assert "404" in responses
    assert "403" not in responses


def test_update_profile_rejects_unknown_fields_on_both_sides() -> None:
    """The push field is `fcm_token`; a lenient body would swallow a `device_token` typo."""
    assert _inline_body("/users/me", "patch").get("additionalProperties") is False
    assert UpdateProfileRequest.model_config.get("extra") == "forbid"


def test_update_profile_length_bounds_match_contract() -> None:
    body = _inline_body("/users/me", "patch")["properties"]
    for field in ("name", "fcm_token"):
        constraints = {type(m).__name__: m for m in UpdateProfileRequest.model_fields[field].metadata}
        assert body[field]["minLength"] == constraints["MinLen"].min_length, field
        assert body[field]["maxLength"] == constraints["MaxLen"].max_length, field


def test_notification_responses_expose_no_delivery_bookkeeping_or_device_token() -> None:
    for model in (NotificationOut, UserProfile):
        leaked = {"sent_at", "retry_count", "user_id", "fcm_token", "device_token"} & set(model.model_fields)
        assert not leaked, f"{model.__name__} exposes {sorted(leaked)}"


# ── Admin ───────────────────────────────────────────────────────────────

ADMIN_OPERATIONS = [(p, m, op) for p, m, op in _operations() if p.startswith("/admin/")]


def test_admin_surface_is_what_is_implemented() -> None:
    """Pinned so an admin route cannot be added to one side and not the other."""
    assert {(m, p) for p, m, _ in ADMIN_OPERATIONS} == {
        ("get", "/admin/authority-users"),
        ("post", "/admin/authority-users"),
        ("patch", "/admin/authority-users/{user_id}/deactivate"),
        ("get", "/admin/departments"),
        ("post", "/admin/departments"),
        ("patch", "/admin/departments/{department_id}"),
        ("get", "/admin/zones"),
        ("post", "/admin/zones"),
        ("get", "/admin/system/stats"),
    }


@pytest.mark.parametrize(
    ("path", "method", "operation"), ADMIN_OPERATIONS, ids=[f"{m.upper()} {p}" for p, m, _ in ADMIN_OPERATIONS]
)
def test_admin_operations_require_a_bearer_token_and_document_401_403(
    path: str, method: str, operation: dict[str, Any]
) -> None:
    assert operation.get("security") == [{"BearerAuth": []}], f"{method.upper()} {path}"
    assert {"401", "403"} <= set(operation["responses"]), f"{method.upper()} {path}"


def test_create_authority_requires_an_initial_password() -> None:
    """No email delivery exists, so an account created without a password could never log in."""
    assert "password" in COMPONENTS["CreateAuthorityRequest"]["required"]
    assert CreateAuthorityRequest.model_fields["password"].is_required()


def test_authority_user_carries_both_identifiers() -> None:
    """`id` feeds assignIssue, `user_id` feeds deactivation; dropping either breaks a dashboard flow."""
    assert {"id", "user_id"} <= set(COMPONENTS["AuthorityUser"]["required"])
    deactivate = PATHS["/admin/authority-users/{user_id}/deactivate"]["patch"]
    assert [p["name"] for p in deactivate["parameters"]] == ["user_id"]


def test_geojson_polygon_type_enum_matches_contract() -> None:
    assert COMPONENTS["GeoJSONPolygon"]["properties"]["type"]["enum"] == ["Polygon"]


def test_department_thresholds_are_at_least_one_in_the_contract() -> None:
    """D-1 / task 4.15a: neither threshold may be set below 1, on create or update."""
    for component in ("CreateDepartmentRequest", "UpdateDepartmentRequest"):
        for field in ("sla_hours", "upvote_alert_threshold"):
            assert COMPONENTS[component]["properties"][field]["minimum"] == 1, f"{component}.{field}"
            assert UpdateDepartmentRequest.model_fields[field].metadata, f"{field} has no bound on the model"


# ── Analytics (task 1.29, SLA breaches, CSV export) ─────────────────────

ANALYTICS_OPERATIONS = [(p, m, op) for p, m, op in _operations() if p.startswith("/analytics/")]


def _contract_query_params(operation: dict[str, Any]) -> set[str]:
    """Query parameter names of an operation, resolving `components.parameters` refs."""
    shared = _spec["components"]["parameters"]
    return {
        (shared[p["$ref"].split("/")[-1]] if "$ref" in p else p)["name"]
        for p in operation.get("parameters", [])
        if (shared[p["$ref"].split("/")[-1]] if "$ref" in p else p)["in"] == "query"
    }


def test_all_five_analytics_operations_are_contracted() -> None:
    assert {p for p, _, _ in ANALYTICS_OPERATIONS} == {
        "/analytics/summary",
        "/analytics/heatmap",
        "/analytics/resolution-times",
        "/analytics/sla-breaches",
        "/analytics/export",
    }


@pytest.mark.parametrize(
    ("path", "method", "operation"), ANALYTICS_OPERATIONS, ids=[p for p, _, _ in ANALYTICS_OPERATIONS]
)
def test_analytics_operations_are_staff_only(path: str, method: str, operation: dict[str, Any]) -> None:
    assert operation.get("security") == [{"BearerAuth": []}], f"{method.upper()} {path}"
    assert {"401", "403"} <= set(operation["responses"]), f"{method.upper()} {path}"


@pytest.mark.parametrize(
    ("path", "method", "operation"), ANALYTICS_OPERATIONS, ids=[p for p, _, _ in ANALYTICS_OPERATIONS]
)
def test_analytics_query_parameters_match_the_routes(path: str, method: str, operation: dict[str, Any]) -> None:
    """The contract and the handler signatures must name the same query parameters."""
    from app.main import create_app

    generated = create_app().openapi()["paths"][f"/v1{path}"][method]
    declared = {p["name"] for p in generated.get("parameters", []) if p["in"] == "query"}
    assert _contract_query_params(operation) == declared


def test_analytics_enums_match_contract() -> None:
    export = PATHS["/analytics/export"]["get"]
    assert [m.value for m in ExportFormat] == export["parameters"][0]["schema"]["enum"]
    times = {p["name"]: p for p in PATHS["/analytics/resolution-times"]["get"]["parameters"] if "name" in p}
    assert [m.value for m in ResolutionGroupBy] == times["group_by"]["schema"]["enum"]
    assert [m.value for m in TrendInterval] == times["interval"]["schema"]["enum"]


def test_pdf_export_is_documented_as_not_implemented() -> None:
    """`pdf` stays in the enum (task 3.25 targets it) but is a documented 501, not a fake file."""
    responses = PATHS["/analytics/export"]["get"]["responses"]
    assert "501" in responses
    assert "EXPORT_FORMAT_NOT_SUPPORTED" in responses["501"]["description"]
    assert "text/csv" in responses["200"]["content"]


def test_nullable_analytics_figures_are_still_required() -> None:
    """A missing average is `null`, never an absent key and never 0."""
    for field in ("avg_resolution_hours", "median_resolution_hours"):
        assert field in COMPONENTS["AnalyticsSummary"]["required"]
        assert COMPONENTS["AnalyticsSummary"]["properties"][field]["nullable"] is True


# ── Issues: responses, scope, staff picker, departments, rate limit ─────


def _generated_operation(path: str, method: str) -> dict[str, Any]:
    """The operation as the running app publishes it at /openapi.json."""
    from app.main import create_app

    return create_app().openapi()["paths"][f"/v1{path}"][method]


def _generated_component(ref: str) -> dict[str, Any]:
    """A component schema as the running app publishes it, by `$ref`."""
    from app.main import create_app

    return create_app().openapi()["components"]["schemas"][ref.split("/")[-1]]


def test_issue_list_and_nearby_document_what_the_handlers_return() -> None:
    """`GET /issues` returns summaries and `/nearby` a bare array — the contract once said otherwise for both."""
    listing = PATHS["/issues"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert listing["$ref"] == "#/components/schemas/PaginatedIssueSummaryResponse"

    nearby = PATHS["/issues/nearby"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert nearby["type"] == "array"
    assert nearby["items"]["$ref"] == "#/components/schemas/NearbyIssue"


def test_stale_issue_shapes_are_gone() -> None:
    """`PaginatedIssueResponse` (items: IssueDetail) described no real endpoint; AI fields are cut (D-2)."""
    assert "PaginatedIssueResponse" not in COMPONENTS
    assert "PaginatedIssueResponse" not in OPENAPI_TEXT
    for name in ("CreateIssueResponse", "IssueDetail", "IssueSummary", "NearbyIssue"):
        stale = {f for f in COMPONENTS[name]["properties"] if f.startswith("ai_")}
        assert not stale, f"{name} still documents {sorted(stale)}"


@pytest.mark.parametrize("path", ["/issues", "/issues/nearby"])
def test_issue_query_parameters_match_the_routes(path: str) -> None:
    declared = {p["name"] for p in _generated_operation(path, "get").get("parameters", []) if p["in"] == "query"}
    assert _contract_query_params(PATHS[path]["get"]) == declared


def test_create_issue_form_matches_contract() -> None:
    """`POST /issues` is multipart, so it has no model to pin; pin the route signature instead."""
    body = _generated_operation("/issues", "post")["requestBody"]["content"]["multipart/form-data"]["schema"]
    generated = _generated_component(body["$ref"])
    contract = COMPONENTS["CreateIssueRequest"]
    assert set(contract["properties"]) == set(generated["properties"])
    assert set(contract["required"]) == set(generated["required"])


STAFF_ISSUE_OPERATIONS = [
    ("/issues/{issue_id}/status", "patch"),
    ("/issues/{issue_id}/assign", "patch"),
    ("/issues/{issue_id}/assignable-staff", "get"),
]


@pytest.mark.parametrize(("path", "method"), STAFF_ISSUE_OPERATIONS)
def test_staff_issue_operations_answer_out_of_scope_with_the_not_found_404(path: str, method: str) -> None:
    """Out of jurisdiction must read as "no such issue", never as a 403 that confirms it exists."""
    operation = PATHS[path][method]
    assert operation["security"] == [{"BearerAuth": []}]
    assert operation["responses"]["404"] == {"$ref": "#/components/responses/IssueNotFoundOrOutOfScope"}
    assert operation["responses"]["403"] == {"$ref": "#/components/responses/StaffOnly"}
    assert "401" in operation["responses"]


def test_assign_documents_the_ineligible_assignee_code() -> None:
    assert "ASSIGNEE_NOT_ELIGIBLE" in PATHS["/issues/{issue_id}/assign"]["patch"]["responses"]["400"]["description"]


def test_assignable_staff_exposes_no_contact_details() -> None:
    """Every authority can read the picker; it identifies colleagues, it does not reach them."""
    for fields in (set(AssignableStaff.model_fields), set(COMPONENTS["AssignableStaff"]["properties"])):
        assert not fields & {"email", "phone", "employee_id", "user_id"}


def test_staff_department_read_is_documented() -> None:
    operation = PATHS["/departments"]["get"]
    assert operation["security"] == [{"BearerAuth": []}]
    assert {"401", "403"} <= set(operation["responses"])
    schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema["$ref"] == "#/components/schemas/DepartmentSummaryList"


def test_issue_submission_documents_the_429_and_retry_after() -> None:
    too_many = PATHS["/issues"]["post"]["responses"]["429"]
    assert "Retry-After" in too_many["headers"]
    assert too_many["content"]["application/json"]["schema"]["$ref"] == "#/components/schemas/ErrorResponse"


def test_logout_documents_the_optional_fcm_token() -> None:
    """D-3: still no required field, so an absent or empty body stays a 204."""
    body = _inline_body("/auth/logout", "post")
    assert "fcm_token" in body["properties"]
    assert "required" not in body
