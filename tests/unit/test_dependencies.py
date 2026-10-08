"""Behavioural tests for `app/dependencies.py` and the session it wires in.

`app/dependencies.py` was at 0% coverage. It is three lines of wiring, but the
wiring is load-bearing: `oauth2_scheme` is constructed with `auto_error=False`,
which is what allows `get_current_user_optional` to return `None` for anonymous
callers instead of FastAPI raising a 401 before any application code runs. If
that flag ever flips, every optional-auth endpoint starts rejecting anonymous
traffic and no test elsewhere would notice.

`get_db` lives in `app/database.py` but is the dependency this module exposes as
`DBSession`, and it was equally unexecuted, so its commit/rollback/close
contract is covered here too — against a mocked `async_sessionmaker`, so no
database is needed.
"""

from __future__ import annotations

from typing import Annotated, Any, get_args, get_origin
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.params import Depends as DependsClass
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app.config import settings
from app.database import get_db
from app.dependencies import DBSession, OptionalToken, oauth2_scheme

BEARER_TOKEN = "eyJhbGciOiJSUzI1NiJ9.payload.signature"


def _request(**headers: str) -> Request:
    """A minimal ASGI request carrying the given headers."""
    raw = [(key.lower().replace("_", "-").encode(), value.encode()) for key, value in headers.items()]
    return Request({"type": "http", "method": "GET", "path": "/", "headers": raw})


# ── oauth2_scheme configuration ─────────────────────────────────────────


def test_scheme_is_a_bearer_scheme_that_does_not_auto_error() -> None:
    """`auto_error=False` is what makes anonymous access possible at all."""
    assert isinstance(oauth2_scheme, OAuth2PasswordBearer)
    assert oauth2_scheme.auto_error is False


def test_token_url_matches_the_versioned_login_route() -> None:
    """Drift here only shows up as a broken 'Authorize' button in the docs."""
    flows = oauth2_scheme.model.flows
    assert flows.password.tokenUrl == "/v1/auth/login"
    assert flows.password.tokenUrl == f"{settings.API_V1_PREFIX}/auth/login"


# ── oauth2_scheme extraction behaviour ──────────────────────────────────


async def test_absent_authorization_header_yields_none_not_an_error() -> None:
    assert await oauth2_scheme(_request()) is None


async def test_bearer_token_is_extracted_without_the_scheme_prefix() -> None:
    assert await oauth2_scheme(_request(authorization=f"Bearer {BEARER_TOKEN}")) == BEARER_TOKEN


async def test_the_bearer_keyword_is_case_insensitive() -> None:
    """RFC 7235 says the scheme is case-insensitive; clients do send `bearer`."""
    assert await oauth2_scheme(_request(authorization=f"bearer {BEARER_TOKEN}")) == BEARER_TOKEN


@pytest.mark.parametrize(
    "header",
    [
        "Basic dXNlcjpwYXNz",
        "Digest username=weft",
        BEARER_TOKEN,  # no scheme at all
        "",
    ],
    ids=["basic", "digest", "bare-token", "empty"],
)
async def test_non_bearer_credentials_are_ignored(header: str) -> None:
    """A non-Bearer credential must arrive as `None` — never be passed through
    to `decode_jwt` as if it were a token."""
    assert await oauth2_scheme(_request(authorization=header)) is None


# ── Dependency type aliases ─────────────────────────────────────────────


def _dependency_of(alias: Any) -> Any:
    """Pull the callable out of `Annotated[T, Depends(callable)]`."""
    assert get_origin(alias) is Annotated
    marker = get_args(alias)[1]
    assert isinstance(marker, DependsClass)
    return marker.dependency


def test_db_session_alias_injects_get_db() -> None:
    assert get_args(DBSession)[0] is AsyncSession
    assert _dependency_of(DBSession) is get_db


def test_optional_token_alias_injects_the_oauth2_scheme() -> None:
    """`str | None`, not `str`: the annotation is half of what tells FastAPI a
    request without credentials is still valid."""
    assert _dependency_of(OptionalToken) is oauth2_scheme
    assert type(None) in get_args(get_args(OptionalToken)[0])


# ── get_db — the session `DBSession` resolves to ────────────────────────


class _FakeSessionContext:
    """Stands in for `async_session_factory()`'s async context manager."""

    def __init__(self, session: AsyncMock) -> None:
        self.session = session
        self.exited = False

    async def __aenter__(self) -> AsyncMock:
        return self.session

    async def __aexit__(self, *exc_info: object) -> bool:
        self.exited = True
        return False


def _patched_factory() -> tuple[_FakeSessionContext, AsyncMock]:
    session = AsyncMock(spec=AsyncSession)
    return _FakeSessionContext(session), session


async def test_get_db_yields_a_session_and_commits_on_success() -> None:
    context, session = _patched_factory()

    with patch("app.database.async_session_factory", MagicMock(return_value=context)):
        agen = get_db()
        yielded = await anext(agen)

        assert yielded is session
        session.commit.assert_not_awaited()

        with pytest.raises(StopAsyncIteration):
            await anext(agen)

    session.commit.assert_awaited_once()
    session.rollback.assert_not_awaited()
    assert context.exited, "the session context manager must close the session"


async def test_get_db_rolls_back_and_re_raises_on_failure() -> None:
    """A failed request must never leave a half-applied transaction, and the
    error must keep propagating so the caller still sees the failure."""
    context, session = _patched_factory()
    boom = RuntimeError("handler exploded")

    with patch("app.database.async_session_factory", MagicMock(return_value=context)):
        agen = get_db()
        await anext(agen)

        with pytest.raises(RuntimeError, match="handler exploded"):
            await agen.athrow(boom)

    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()
    assert context.exited


async def test_get_db_closes_the_session_when_the_client_disconnects() -> None:
    """An abandoned generator still has to hand its connection back to the
    pool, or the pool bleeds out under load."""
    context, session = _patched_factory()

    with patch("app.database.async_session_factory", MagicMock(return_value=context)):
        agen = get_db()
        await anext(agen)
        await agen.aclose()

    assert context.exited
    session.commit.assert_not_awaited()
