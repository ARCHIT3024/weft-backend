"""Unit tests for `app/core/push.py` — the FCM HTTP v1 sender and its selection.

No network and no real waiting: every FCM response comes from an
`httpx.MockTransport`, the access token from a stub provider, and the backoff
`sleep` is recorded rather than awaited. What is being pinned is the retry
classification, because each branch has a cost if it is wrong:

* retrying a permanent failure wastes three requests per device per event;
* *not* retrying a transient one loses a notification FCM would have taken;
* calling a live token dead deletes a user's device registration, and the
  `INVALID_ARGUMENT` case can do that to every user at once.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.core import push
from app.core.push import (
    FcmV1PushSender,
    LoggingPushSender,
    PushMessage,
    PushOutcome,
    PushResult,
    PushSender,
    ServiceAccountTokenProvider,
    redact_token,
)

TOKEN = "device-token-abcdef123456"
MESSAGE = PushMessage(title="Update on ISS-2026-X", body="Your pothole report is being addressed.", data={"a": "b"})


class StubTokenProvider:
    """Hands out a fixed access token and records invalidations."""

    def __init__(self, token: str = "access-1", fail_times: int = 0) -> None:  # noqa: S107 — a stub, not a secret
        self.token = token
        self.calls = 0
        self.invalidations = 0
        self._fail_times = fail_times

    async def __call__(self) -> str:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise RuntimeError("oauth endpoint unreachable")
        return self.token

    def invalidate(self) -> None:
        self.invalidations += 1


def _fcm_error(status: int, fcm_status: str, error_code: str | None = None, message: str = "") -> httpx.Response:
    details = [{"@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError", "errorCode": error_code}]
    body = {
        "error": {"code": status, "status": fcm_status, "message": message, "details": details if error_code else []}
    }
    return httpx.Response(status, json=body)


def _ok() -> httpx.Response:
    return httpx.Response(200, json={"name": "projects/weft-test/messages/0:123"})


class Scripted:
    """An `httpx.MockTransport` handler that replays responses in order."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self._responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def _sender(script: Scripted, provider: StubTokenProvider | None = None, **kwargs: Any) -> tuple[FcmV1PushSender, list]:
    sleeps: list[float] = []

    async def _sleep(seconds: float) -> None:
        sleeps.append(seconds)

    sender = FcmV1PushSender(
        "weft-test",
        provider or StubTokenProvider(),
        transport=httpx.MockTransport(script),
        sleep=_sleep,
        **kwargs,
    )
    return sender, sleeps


# ── Happy path and request shape ────────────────────────────────────────


class TestRequest:
    async def test_delivers_on_first_success(self) -> None:
        script = Scripted(_ok())
        sender, sleeps = _sender(script)

        result = await sender.send(TOKEN, MESSAGE)

        assert result == PushResult(outcome=PushOutcome.DELIVERED, attempts=1)
        assert result.failed_attempts == 0
        assert sleeps == []

    async def test_posts_to_the_v1_endpoint_with_a_bearer_token(self) -> None:
        script = Scripted(_ok())
        sender, _ = _sender(script, StubTokenProvider(token="ya29.secret"))

        await sender.send(TOKEN, MESSAGE)

        request = script.requests[0]
        assert request.method == "POST"
        assert str(request.url) == "https://fcm.googleapis.com/v1/projects/weft-test/messages:send"
        assert request.headers["Authorization"] == "Bearer ya29.secret"

    async def test_payload_matches_the_trd_shape(self) -> None:
        script = Scripted(_ok())
        sender, _ = _sender(script)

        await sender.send(TOKEN, MESSAGE)

        body = json.loads(script.requests[0].content)["message"]
        assert body["token"] == TOKEN
        assert body["notification"] == {"title": MESSAGE.title, "body": MESSAGE.body}
        assert body["data"] == {"a": "b"}
        assert body["android"] == {"priority": "high"}
        assert body["apns"] == {"headers": {"apns-priority": "10"}}

    def test_data_values_are_coerced_to_strings(self) -> None:
        """FCM's `data` is map<string, string>; a non-string is an INVALID_ARGUMENT."""
        payload = FcmV1PushSender.build_payload(TOKEN, PushMessage(title="t", body="b", data={"n": 3}))  # type: ignore[dict-item]
        assert payload["message"]["data"] == {"n": "3"}

    def test_project_id_is_required(self) -> None:
        with pytest.raises(ValueError):
            FcmV1PushSender("", StubTokenProvider())


# ── Retry: transient failures ───────────────────────────────────────────


class TestRetry:
    @pytest.mark.parametrize("status", [500, 502, 503, 504, 429])
    async def test_transient_status_is_retried_then_delivered(self, status: int) -> None:
        script = Scripted(httpx.Response(status), _ok())
        sender, sleeps = _sender(script)

        result = await sender.send(TOKEN, MESSAGE)

        assert result.outcome is PushOutcome.DELIVERED
        assert result.attempts == 2
        assert result.failed_attempts == 1
        assert len(sleeps) == 1

    async def test_network_error_is_retried(self) -> None:
        script = Scripted(httpx.ConnectError("refused"), _ok())
        sender, _ = _sender(script)

        result = await sender.send(TOKEN, MESSAGE)

        assert result.outcome is PushOutcome.DELIVERED
        assert result.attempts == 2

    async def test_access_token_failure_is_retried(self) -> None:
        provider = StubTokenProvider(fail_times=1)
        script = Scripted(_ok())
        sender, _ = _sender(script, provider)

        result = await sender.send(TOKEN, MESSAGE)

        assert result.outcome is PushOutcome.DELIVERED
        assert result.attempts == 2
        assert len(script.requests) == 1, "no FCM request is made without an access token"

    async def test_gives_up_after_three_attempts_with_exponential_backoff(self) -> None:
        script = Scripted(httpx.Response(503), httpx.Response(503), httpx.Response(503))
        sender, sleeps = _sender(script, base_delay_seconds=1.0)

        result = await sender.send(TOKEN, MESSAGE)

        assert result.outcome is PushOutcome.FAILED
        assert result.attempts == 3
        assert result.failed_attempts == 3
        assert len(script.requests) == 3
        assert sleeps == [1.0, 2.0], "doubles between attempts; no sleep after the last"

    async def test_retry_after_header_is_honoured_but_capped(self) -> None:
        script = Scripted(
            httpx.Response(429, headers={"Retry-After": "4"}),
            httpx.Response(429, headers={"Retry-After": "3600"}),
            _ok(),
        )
        sender, sleeps = _sender(script, base_delay_seconds=0.5)

        await sender.send(TOKEN, MESSAGE)

        assert sleeps == [4.0, push.MAX_RETRY_DELAY_SECONDS]

    async def test_expired_access_token_401_invalidates_and_retries(self) -> None:
        provider = StubTokenProvider()
        script = Scripted(_fcm_error(401, "UNAUTHENTICATED"), _ok())
        sender, _ = _sender(script, provider)

        result = await sender.send(TOKEN, MESSAGE)

        assert result.outcome is PushOutcome.DELIVERED
        assert provider.invalidations == 1

    async def test_non_json_error_body_is_survivable(self) -> None:
        """A proxy's HTML 502 must classify, not raise."""
        script = Scripted(httpx.Response(502, text="<html>Bad Gateway</html>"), _ok())
        sender, _ = _sender(script)
        assert (await sender.send(TOKEN, MESSAGE)).outcome is PushOutcome.DELIVERED

    async def test_max_attempts_is_configurable(self) -> None:
        script = Scripted(httpx.Response(500))
        sender, sleeps = _sender(script, max_attempts=1)

        result = await sender.send(TOKEN, MESSAGE)

        assert result.attempts == 1
        assert sleeps == []


# ── Permanent failures: never retried ───────────────────────────────────


class TestPermanent:
    async def test_unregistered_token_is_invalid_and_not_retried(self) -> None:
        script = Scripted(_fcm_error(404, "NOT_FOUND", "UNREGISTERED", "Requested entity was not found."))
        sender, sleeps = _sender(script)

        result = await sender.send(TOKEN, MESSAGE)

        assert result.outcome is PushOutcome.INVALID_TOKEN
        assert result.attempts == 1
        assert sleeps == []

    async def test_bare_404_is_an_invalid_token(self) -> None:
        script = Scripted(httpx.Response(404))
        sender, _ = _sender(script)
        assert (await sender.send(TOKEN, MESSAGE)).outcome is PushOutcome.INVALID_TOKEN

    async def test_invalid_argument_naming_the_token_is_an_invalid_token(self) -> None:
        script = Scripted(
            _fcm_error(
                400,
                "INVALID_ARGUMENT",
                "INVALID_ARGUMENT",
                "The registration token is not a valid FCM registration token",
            )
        )
        sender, _ = _sender(script)

        result = await sender.send(TOKEN, MESSAGE)

        assert result.outcome is PushOutcome.INVALID_TOKEN
        assert result.attempts == 1

    async def test_invalid_argument_about_the_payload_keeps_the_token(self) -> None:
        """A payload bug must not be read as "every token is dead".

        Otherwise one malformed message deletes the device registration of
        every user it is sent to.
        """
        script = Scripted(_fcm_error(400, "INVALID_ARGUMENT", "INVALID_ARGUMENT", "Invalid value at 'message.data[0]'"))
        sender, sleeps = _sender(script)

        result = await sender.send(TOKEN, MESSAGE)

        assert result.outcome is PushOutcome.FAILED
        assert result.attempts == 1, "a 400 is not retried"
        assert sleeps == []

    @pytest.mark.parametrize(
        ("status", "fcm_status", "code"),
        [
            (403, "PERMISSION_DENIED", "SENDER_ID_MISMATCH"),
            (403, "PERMISSION_DENIED", None),
            (400, "FAILED_PRECONDITION", None),
        ],
    )
    async def test_other_client_errors_fail_without_retry_and_keep_the_token(
        self, status: int, fcm_status: str, code: str | None
    ) -> None:
        script = Scripted(_fcm_error(status, fcm_status, code))
        sender, _ = _sender(script)

        result = await sender.send(TOKEN, MESSAGE)

        assert result.outcome is PushOutcome.FAILED
        assert result.attempts == 1
        assert result.error is not None


# ── Access-token provider ───────────────────────────────────────────────


class FakeCredentials:
    """Just the surface `ServiceAccountTokenProvider` touches."""

    def __init__(self) -> None:
        self.token: str | None = None
        self.refreshes = 0

    @property
    def valid(self) -> bool:
        return self.token is not None

    def refresh(self, request: Any) -> None:
        self.refreshes += 1
        self.token = f"minted-{self.refreshes}"


class TestTokenProvider:
    async def test_mints_once_and_caches(self) -> None:
        credentials = FakeCredentials()
        provider = ServiceAccountTokenProvider(credentials)

        assert await provider() == "minted-1"
        assert await provider() == "minted-1"
        assert credentials.refreshes == 1

    async def test_invalidate_forces_a_fresh_token(self) -> None:
        credentials = FakeCredentials()
        provider = ServiceAccountTokenProvider(credentials)

        await provider()
        provider.invalidate()

        assert await provider() == "minted-2"


# ── Development sender and selection ────────────────────────────────────


class TestSelection:
    async def test_logging_sender_reports_delivery(self) -> None:
        result = await LoggingPushSender().send(TOKEN, MESSAGE)
        assert result.outcome is PushOutcome.DELIVERED

    def test_both_senders_satisfy_the_protocol(self) -> None:
        assert isinstance(LoggingPushSender(), PushSender)
        assert isinstance(FcmV1PushSender("p", StubTokenProvider()), PushSender)

    def test_unconfigured_fcm_selects_the_logging_sender(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(push.settings, "FCM_SERVICE_ACCOUNT_JSON", "")
        assert isinstance(push.build_push_sender(), LoggingPushSender)

    def test_malformed_credentials_are_loud(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A set-but-broken credential must not quietly become "logged only"."""
        monkeypatch.setattr(push.settings, "FCM_SERVICE_ACCOUNT_JSON", '{"type": "authorized_user"}')
        with pytest.raises(ValueError):
            push.build_push_sender()

    def test_unreadable_credential_path_is_loud(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        monkeypatch.setattr(push.settings, "FCM_SERVICE_ACCOUNT_JSON", str(tmp_path / "missing.json"))
        with pytest.raises(OSError):
            push.build_push_sender()

    def test_service_account_selects_fcm_with_project_from_the_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}

        def _fake_from_info(info: dict, scopes: list[str]) -> FakeCredentials:
            captured["info"], captured["scopes"] = info, scopes
            return FakeCredentials()

        from google.oauth2 import service_account

        monkeypatch.setattr(service_account.Credentials, "from_service_account_info", staticmethod(_fake_from_info))
        monkeypatch.setattr(push.settings, "FCM_PROJECT_ID", "")
        monkeypatch.setattr(
            push.settings,
            "FCM_SERVICE_ACCOUNT_JSON",
            json.dumps({"type": "service_account", "project_id": "weft-from-key"}),
        )

        sender = push.build_push_sender()

        assert isinstance(sender, FcmV1PushSender)
        assert "weft-from-key" in sender._url
        assert captured["scopes"] == [push.FCM_OAUTH_SCOPE]

    def test_redact_token_keeps_only_a_suffix(self) -> None:
        assert redact_token(TOKEN) == "…123456"
        assert TOKEN[:10] not in redact_token(TOKEN)
        assert redact_token("short") == "…"
