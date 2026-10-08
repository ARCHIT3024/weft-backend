"""Behavioural tests for `app/core/permissions.py` — authn and RBAC.

This module was at 0% coverage: `get_current_user`, `get_current_user_optional`
and `require_role` had never been executed. The property that matters most —
TRD Section 6's rule that a *cryptographically valid* token must still be
refused when `users.is_active = FALSE` — was therefore entirely unproven, even
though it is the only reason `get_current_user` pays for a DB round trip at
all.

The dependency callables are invoked directly with explicit keyword arguments,
so FastAPI's `Depends(...)` defaults never come into play and no app, request
or database is needed. `db.get` is the seam: an `AsyncMock` stands in for the
`AsyncSession`, which also lets each test assert whether the lookup happened.

Tokens are real RS256 tokens signed with the configured key — nothing about the
crypto is faked here.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ForbiddenError, UnauthorizedError
from app.core.permissions import (
    Role,
    get_current_user,
    get_current_user_optional,
    require_admin,
    require_authority,
    require_citizen,
    require_role,
)
from app.core.security import create_access_token
from app.models.user import User

USER_ID = str(uuid.UUID("33333333-3333-3333-3333-333333333333"))


# ── Helpers ─────────────────────────────────────────────────────────────


def _user(role: str = "CITIZEN", *, is_active: bool = True, user_id: str = USER_ID) -> User:
    """An in-memory `User` — constructing the ORM object touches no database."""
    return User(id=uuid.UUID(user_id), email="user@test.com", role=role, is_active=is_active)


def _db(returns: User | None) -> AsyncSession:
    """An `AsyncSession` double whose `.get()` resolves to `returns`."""
    db = MagicMock(spec=AsyncSession)
    db.get = AsyncMock(return_value=returns)
    return db


def _token(role: str = "CITIZEN", user_id: str = USER_ID) -> str:
    return create_access_token(user_id=user_id, role=role, email="user@test.com")


# ── get_current_user ────────────────────────────────────────────────────


async def test_missing_token_is_rejected_without_touching_the_database() -> None:
    db = _db(_user())

    with pytest.raises(UnauthorizedError) as exc_info:
        await get_current_user(token=None, db=db)

    assert exc_info.value.status_code == 401
    db.get.assert_not_awaited()


@pytest.mark.parametrize("bad_token", ["", "garbage", "a.b.c"], ids=["empty", "garbage", "three-segments"])
async def test_invalid_token_is_rejected_without_touching_the_database(bad_token: str) -> None:
    db = _db(_user())

    with pytest.raises(UnauthorizedError):
        await get_current_user(token=bad_token, db=db)

    db.get.assert_not_awaited()


async def test_valid_token_for_an_active_user_returns_that_user() -> None:
    expected = _user("CITIZEN")
    db = _db(expected)

    result = await get_current_user(token=_token("CITIZEN"), db=db)

    assert result is expected


async def test_lookup_uses_the_subject_claim_as_the_primary_key() -> None:
    """The DB row is fetched by `sub`, not by anything client-controlled."""
    other_id = "44444444-4444-4444-4444-444444444444"
    db = _db(_user(user_id=other_id))

    await get_current_user(token=_token(user_id=other_id), db=db)

    db.get.assert_awaited_once_with(User, other_id)


async def test_deactivated_user_is_rejected_despite_a_valid_token() -> None:
    """TRD Section 6, the whole point of the per-request DB lookup.

    A banned or deleted account keeps holding a signed, unexpired JWT until it
    expires (24h by default). Trusting the signature alone would leave that
    account fully functional for a day after deactivation.
    """
    db = _db(_user("ADMIN", is_active=False))

    with pytest.raises(UnauthorizedError) as exc_info:
        await get_current_user(token=_token("ADMIN"), db=db)

    assert exc_info.value.status_code == 401
    assert "deactivated" in exc_info.value.error_message.lower()
    db.get.assert_awaited_once()


async def test_user_missing_from_the_database_is_rejected() -> None:
    """Hard-deleted account, or a token minted against another environment."""
    db = _db(None)

    with pytest.raises(UnauthorizedError) as exc_info:
        await get_current_user(token=_token(), db=db)

    assert exc_info.value.status_code == 401


async def test_expired_token_is_rejected_before_the_database_lookup() -> None:
    from app.config import settings

    original = settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES
    settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES = -1
    try:
        expired = _token()
    finally:
        settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES = original

    db = _db(_user())

    with pytest.raises(UnauthorizedError):
        await get_current_user(token=expired, db=db)

    db.get.assert_not_awaited()


# ── get_current_user_optional ───────────────────────────────────────────


async def test_optional_returns_none_when_no_token_is_present() -> None:
    """Anonymous access must not raise on endpoints that allow it."""
    db = _db(_user())

    assert await get_current_user_optional(token=None, db=db) is None
    db.get.assert_not_awaited()


async def test_optional_returns_the_user_when_the_token_is_valid() -> None:
    expected = _user()
    db = _db(expected)

    assert await get_current_user_optional(token=_token(), db=db) is expected


async def test_optional_returns_none_for_a_malformed_token() -> None:
    """A broken credential degrades to anonymous rather than 500-ing; it must
    never be honoured as if it were valid."""
    db = _db(_user())

    assert await get_current_user_optional(token="not-a-jwt", db=db) is None


async def test_optional_returns_none_for_a_deactivated_user() -> None:
    """A banned account is anonymous on optional-auth endpoints — it must not
    come back as an authenticated principal."""
    db = _db(_user(is_active=False))

    assert await get_current_user_optional(token=_token(), db=db) is None


# ── require_role ────────────────────────────────────────────────────────


async def test_require_role_allows_a_matching_role() -> None:
    expected = _user("ADMIN")
    guard = require_role(Role.ADMIN)

    assert await guard(token=_token("ADMIN"), db=_db(expected)) is expected


async def test_require_role_rejects_a_mismatched_role() -> None:
    guard = require_role(Role.ADMIN)

    with pytest.raises(ForbiddenError) as exc_info:
        await guard(token=_token("CITIZEN"), db=_db(_user("CITIZEN")))

    assert exc_info.value.status_code == 403
    assert exc_info.value.code == "FORBIDDEN"


@pytest.mark.parametrize("role", ["AUTHORITY", "ADMIN"])
async def test_require_role_accepts_any_of_several_roles(role: str) -> None:
    guard = require_role(Role.AUTHORITY, Role.ADMIN)
    expected = _user(role)

    assert await guard(token=_token(role), db=_db(expected)) is expected


async def test_multi_role_guard_still_rejects_everyone_else() -> None:
    guard = require_role(Role.AUTHORITY, Role.ADMIN)

    with pytest.raises(ForbiddenError):
        await guard(token=_token("CITIZEN"), db=_db(_user("CITIZEN")))


@pytest.mark.parametrize(
    "role",
    ["SUPERADMIN", "ADMINISTRATOR", "NOT_ADMIN", "admin", "Admin", "ADMIN ", " ADMIN"],
)
async def test_require_role_matches_the_whole_role_not_a_substring(role: str) -> None:
    """`"ADMIN" in user.role` would let every one of these through; membership
    must be exact-value, case-sensitive equality against the enum."""
    guard = require_role(Role.ADMIN)

    with pytest.raises(ForbiddenError):
        await guard(token=_token(role), db=_db(_user(role)))


async def test_authorisation_failure_names_the_required_roles_only() -> None:
    guard = require_role(Role.AUTHORITY, Role.ADMIN)

    with pytest.raises(ForbiddenError) as exc_info:
        await guard(token=_token("CITIZEN"), db=_db(_user("CITIZEN")))

    assert "AUTHORITY" in exc_info.value.error_message
    assert "ADMIN" in exc_info.value.error_message


async def test_require_role_rejects_an_anonymous_caller_as_401_not_403() -> None:
    """Missing credentials is an authentication failure; conflating it with 403
    tells a client to stop retrying when it should be logging in."""
    guard = require_role(Role.ADMIN)

    with pytest.raises(UnauthorizedError):
        await guard(token=None, db=_db(None))


async def test_require_role_rejects_a_deactivated_privileged_user() -> None:
    """Authentication runs before authorisation: a deactivated ADMIN is 401,
    never an allowed ADMIN."""
    guard = require_role(Role.ADMIN)

    with pytest.raises(UnauthorizedError):
        await guard(token=_token("ADMIN"), db=_db(_user("ADMIN", is_active=False)))


async def test_token_role_claim_cannot_override_the_stored_role() -> None:
    """RBAC is decided by the DB row, not by the client's copy of it. A token
    claiming ADMIN for a user stored as CITIZEN must not pass an admin guard.
    """
    guard = require_role(Role.ADMIN)

    with pytest.raises(ForbiddenError):
        await guard(token=_token("ADMIN"), db=_db(_user("CITIZEN")))


# ── Convenience guards ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("guard", "role", "allowed"),
    [
        (require_citizen, "CITIZEN", True),
        (require_citizen, "AUTHORITY", False),
        (require_citizen, "ADMIN", False),
        (require_authority, "AUTHORITY", True),
        (require_authority, "ADMIN", True),
        (require_authority, "CITIZEN", False),
        (require_admin, "ADMIN", True),
        (require_admin, "AUTHORITY", False),
        (require_admin, "CITIZEN", False),
    ],
)
async def test_convenience_guards_enforce_their_documented_role_sets(guard, role: str, allowed: bool) -> None:
    user = _user(role)
    db = _db(user)

    if allowed:
        assert await guard(token=_token(role), db=db) is user
    else:
        with pytest.raises(ForbiddenError):
            await guard(token=_token(role), db=db)


def test_role_enum_values_match_the_database_enum() -> None:
    """`require_role` compares against `r.value`, and the `user_role` Postgres
    type is what lands in `user.role`; drift here silently locks everyone out.
    """
    assert [r.value for r in Role] == ["CITIZEN", "AUTHORITY", "ADMIN"]
    assert Role.ADMIN == "ADMIN"
    assert set(User.__table__.columns["role"].type.enums) == {r.value for r in Role}
