"""Behavioural tests for `app/core/captcha.py` — reCAPTCHA v3 verification.

This module was at 0% coverage, which for a bot gate means the score threshold
— the only thing it actually decides — had never been evaluated once.

Every test patches `httpx.AsyncClient` inside the module, so nothing here talks
to Google or to anything else: `_FakeAsyncClient` records the request that
*would* have been sent and hands back a canned response. That doubles as the
assertion that the right payload goes to the right URL.

The three tests under "Transport failures" pin down the module's current lack
of error handling rather than endorsing it; all three are called out in the
report.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from app.config import settings
from app.core import captcha as captcha_module
from app.core.captcha import RECAPTCHA_VERIFY_URL, verify_captcha
from app.core.exceptions import BadRequestError

TOKEN = "03AGdBq24-client-supplied-recaptcha-token"


# ── httpx double ────────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, payload: Any = None, json_error: Exception | None = None) -> None:
        self._payload = payload
        self._json_error = json_error

    def json(self) -> Any:
        if self._json_error is not None:
            raise self._json_error
        return self._payload


class _FakeAsyncClient:
    """Stands in for `httpx.AsyncClient`, recording the call it received."""

    def __init__(self, response: _FakeResponse | None = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        self.closed = True
        return False

    async def post(self, url: str, data: dict[str, Any] | None = None) -> _FakeResponse:
        self.calls.append((url, data or {}))
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


@contextmanager
def google_returns(**payload: Any) -> Iterator[_FakeAsyncClient]:
    """Patch the module's httpx client so Google 'responds' with `payload`."""
    client = _FakeAsyncClient(response=_FakeResponse(payload))
    with patch.object(captcha_module.httpx, "AsyncClient", return_value=client):
        yield client


@contextmanager
def google_fails_with(error: Exception) -> Iterator[_FakeAsyncClient]:
    """Patch the module's httpx client so the request raises `error`."""
    client = _FakeAsyncClient(error=error)
    with patch.object(captcha_module.httpx, "AsyncClient", return_value=client):
        yield client


# ── Happy path ──────────────────────────────────────────────────────────


async def test_a_good_token_returns_googles_score() -> None:
    with google_returns(success=True, score=0.9) as client:
        assert await verify_captcha(TOKEN) == 0.9

    assert client.closed, "the AsyncClient context manager must be exited"


async def test_the_token_and_secret_are_posted_to_googles_verify_endpoint() -> None:
    with google_returns(success=True, score=0.9) as client:
        await verify_captcha(TOKEN)

    url, data = client.calls[0]
    assert url == RECAPTCHA_VERIFY_URL == "https://www.google.com/recaptcha/api/siteverify"
    assert data == {"secret": settings.RECAPTCHA_SECRET_KEY, "response": TOKEN}


async def test_exactly_one_request_is_made_per_verification() -> None:
    """No retries: a retry loop against a single-use token just burns latency."""
    with google_returns(success=True, score=1.0) as client:
        await verify_captcha(TOKEN)

    assert len(client.calls) == 1


# ── The threshold ───────────────────────────────────────────────────────


async def test_a_score_below_the_threshold_is_rejected() -> None:
    """The reason this module exists: Google says "verified", but the score
    says "bot", and the low score has to win."""
    with google_returns(success=True, score=0.3), pytest.raises(BadRequestError) as exc_info:
        await verify_captcha(TOKEN)

    assert exc_info.value.status_code == 400
    assert exc_info.value.code == "CAPTCHA_FAILED"
    assert "Automated submission" in exc_info.value.error_message


async def test_a_score_exactly_at_the_threshold_is_accepted() -> None:
    """The comparison is `<`, so the threshold itself must pass."""
    threshold = settings.RECAPTCHA_SCORE_THRESHOLD

    with google_returns(success=True, score=threshold):
        assert await verify_captcha(TOKEN) == threshold


async def test_a_score_a_hair_under_the_threshold_is_rejected() -> None:
    just_under = settings.RECAPTCHA_SCORE_THRESHOLD - 0.01

    with google_returns(success=True, score=just_under), pytest.raises(BadRequestError):
        await verify_captcha(TOKEN)


async def test_the_threshold_is_read_from_settings_not_hardcoded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tightening the gate during an attack must be a config change."""
    monkeypatch.setattr(settings, "RECAPTCHA_SCORE_THRESHOLD", 0.9)

    with google_returns(success=True, score=0.8), pytest.raises(BadRequestError):
        await verify_captcha(TOKEN)

    with google_returns(success=True, score=0.95):
        assert await verify_captcha(TOKEN) == 0.95


async def test_a_missing_score_is_treated_as_the_worst_score() -> None:
    """Failing open on a malformed success body would be a free bot pass."""
    with google_returns(success=True), pytest.raises(BadRequestError) as exc_info:
        await verify_captcha(TOKEN)

    assert exc_info.value.code == "CAPTCHA_FAILED"


# ── Google refuses the token ────────────────────────────────────────────


async def test_success_false_is_rejected() -> None:
    payload = {"success": False, "error-codes": ["invalid-input-response"]}

    with google_returns(**payload), pytest.raises(BadRequestError) as exc_info:
        await verify_captcha(TOKEN)

    assert exc_info.value.code == "CAPTCHA_FAILED"
    assert "verification failed" in exc_info.value.error_message


async def test_success_false_is_rejected_even_with_a_perfect_score() -> None:
    """A caller cannot smuggle a score past a failed verification."""
    with google_returns(success=False, score=1.0), pytest.raises(BadRequestError):
        await verify_captcha(TOKEN)


async def test_an_empty_response_body_is_rejected() -> None:
    """A missing `success` key must fail closed, not default to allowed."""
    with google_returns(), pytest.raises(BadRequestError):
        await verify_captcha(TOKEN)


async def test_the_rejection_does_not_leak_googles_error_codes() -> None:
    """`error-codes` can contain `invalid-input-secret` — a server-side
    misconfiguration that must not be echoed back to the client."""
    payload = {"success": False, "error-codes": ["invalid-input-secret"]}

    with google_returns(**payload), pytest.raises(BadRequestError) as exc_info:
        await verify_captcha(TOKEN)

    assert "invalid-input-secret" not in str(exc_info.value.detail)


# ── Transport failures: current behaviour, flagged in the report ────────


async def test_a_transport_error_reaches_the_caller_unconverted() -> None:
    """BUG PIN: the `httpx` call is not wrapped, so a DNS failure or refused
    connection escapes as a raw `httpx.RequestError`.

    FastAPI turns that into an unhandled 500 with no `error.code`, which breaks
    the documented error envelope, and it means Google being unreachable
    decides the outcome instead of policy. The module should catch
    `httpx.RequestError` and choose deliberately between failing closed
    (400 CAPTCHA_FAILED / 503) and failing open. When it does, this test should
    assert that behaviour instead.
    """
    with google_fails_with(httpx.ConnectError("name resolution failed")), pytest.raises(httpx.ConnectError):
        await verify_captcha(TOKEN)


async def test_a_timeout_reaches_the_caller_unconverted() -> None:
    """The same gap, and the likelier one: `httpx.AsyncClient()` is constructed
    with no `timeout=`, so the wait is bounded only by httpx's own default."""
    with google_fails_with(httpx.ReadTimeout("timed out")), pytest.raises(httpx.ReadTimeout):
        await verify_captcha(TOKEN)


async def test_a_non_json_response_reaches_the_caller_unconverted() -> None:
    """BUG PIN: there is no `raise_for_status()`, so a Google 5xx HTML error
    page is handed straight to `.json()` and blows up as a decode error."""
    client = _FakeAsyncClient(response=_FakeResponse(json_error=ValueError("Expecting value: line 1 column 1")))
    patched = patch.object(captcha_module.httpx, "AsyncClient", return_value=client)

    with patched, pytest.raises(ValueError, match="Expecting value"):
        await verify_captcha(TOKEN)
