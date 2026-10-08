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
    (PaginatedResponse, "PaginatedIssueResponse"),
    (ErrorResponse, "ErrorResponse"),
    (HealthResponse, "HealthResponse"),
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
    assert set(model.model_fields) == {"refresh_token"}
    assert not model.model_fields["refresh_token"].is_required()


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

RESPONSE_MODELS = [RegisterResponse, AuthResponse, TokenResponse, UserProfile]


@pytest.mark.parametrize("model", RESPONSE_MODELS, ids=[m.__name__ for m in RESPONSE_MODELS])
def test_response_models_expose_no_credential_material(model: type) -> None:
    """Refresh tokens are issued in responses by design; passwords never are."""
    leaked = [name for name in model.model_fields if "password" in name.lower() or name.endswith("_hash")]
    assert not leaked, f"{model.__name__} exposes credential field(s): {leaked}"
