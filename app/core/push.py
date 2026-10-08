"""Push transport — FCM HTTP v1 behind a one-method interface.

Same shape as `app/core/storage.py` (D-11). Firebase credentials do not exist
yet, so the seam is `PushSender`: one async method that sends one message to
one device token and reports what happened. Two implementations:

* `LoggingPushSender` — the default whenever `FCM_SERVICE_ACCOUNT_JSON` is
  empty. Logs and reports success. Development and the test suite never need
  Firebase, and a missing credential can never be the thing that breaks a
  status change.
* `FcmV1PushSender` — the real FCM HTTP v1 API, authenticated with a
  short-lived OAuth2 access token minted from the service account.

The sender knows nothing about the database. It *classifies* each outcome —
delivered, the token is dead, or delivery failed — and
`app/services/notification_service.py` decides what that means for the
`notifications` row and the `user_fcm_tokens` row. Keeping the HTTP rules here
and the persistence rules there is what lets the FCM behaviour be unit-tested
against a mocked transport with no database and no network.

**Retry policy.** Only transient failures are retried: HTTP 5xx, 429, a 401
from an access token that expired mid-flight, and network errors. Everything
else is final on the first answer — retrying a 400 just sends the same bad
request three times. A token FCM reports as `UNREGISTERED` (HTTP 404) is
classified `INVALID_TOKEN` so the caller deletes it; that token will never work
again and every future send to it is wasted.

**`INVALID_ARGUMENT` is narrowed deliberately.** FCM returns it both for a
malformed registration token *and* for a malformed message. Treating every
`INVALID_ARGUMENT` as a dead token would mean that one payload bug deletes the
device token of every user it is sent to — silently unregistering the whole
user base from push. Only an `INVALID_ARGUMENT` whose message names the
registration token is classified `INVALID_TOKEN`; any other is a plain
`FAILED` and the token is kept.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

FCM_SEND_URL = "https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
FCM_OAUTH_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"

# Per-request ceiling. A background task has no client waiting on it, but a
# hung connection would still pin the task — and its retries — indefinitely.
FCM_REQUEST_TIMEOUT_SECONDS = 10.0

# A server-supplied Retry-After is honoured, but never beyond this: the retry
# loop runs inside a request's background task, not a durable queue.
MAX_RETRY_DELAY_SECONDS = 10.0

_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


def redact_token(token: str) -> str:
    """Log-safe form of a device token.

    A registration token is the address of one person's phone; anyone holding
    it (and the project's credentials) can push to that device. Logs keep only
    enough to correlate lines.
    """
    return f"…{token[-6:]}" if len(token) > 6 else "…"


class PushOutcome(StrEnum):
    """What happened to one message sent to one device token."""

    DELIVERED = "DELIVERED"  # FCM accepted the message
    INVALID_TOKEN = "INVALID_TOKEN"  # the token is permanently dead — delete it
    FAILED = "FAILED"  # not delivered; the token may still be good


@dataclass(frozen=True)
class PushMessage:
    """A platform-neutral push notification.

    `data` values must be strings: the FCM v1 `data` map is
    `map<string, string>` and rejects anything else with `INVALID_ARGUMENT`.
    """

    title: str
    body: str
    data: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PushResult:
    """Outcome of sending to one token. `attempts` counts every HTTP try."""

    outcome: PushOutcome
    attempts: int
    error: str | None = None

    @property
    def failed_attempts(self) -> int:
        """Tries that did not deliver — what `notifications.retry_count` sums."""
        return self.attempts - 1 if self.outcome is PushOutcome.DELIVERED else self.attempts


@runtime_checkable
class PushSender(Protocol):
    """Send one message to one device. Must not raise for delivery failures."""

    async def send(self, token: str, message: PushMessage) -> PushResult:
        """Deliver `message` to `token` and classify the outcome."""
        ...


# ── Development sender ──────────────────────────────────────────────────


class LoggingPushSender:
    """`PushSender` that delivers nothing and always reports success.

    Selected when FCM is not configured. The `notifications` row is still
    written by the caller, so the in-app notification centre works end to end
    in development; only the phone-buzz is missing.
    """

    async def send(self, token: str, message: PushMessage) -> PushResult:
        logger.info(
            "Push (FCM not configured, logged only) token=%s title=%r data=%s",
            redact_token(token),
            message.title,
            dict(message.data),
        )
        return PushResult(outcome=PushOutcome.DELIVERED, attempts=1)


# ── FCM HTTP v1 sender ──────────────────────────────────────────────────

AccessTokenProvider = Callable[[], Awaitable[str]]


class ServiceAccountTokenProvider:
    """Mints and caches the OAuth2 access token FCM HTTP v1 requires.

    `google-auth` does the JWT-bearer exchange; its credential object caches
    the token and reports `valid` until shortly before expiry, so a refresh
    happens roughly once an hour, not once per push. The refresh is a blocking
    HTTP call, so it runs in a worker thread, and a lock stops a burst of
    concurrent sends from each minting their own token.

    The transport handed to google-auth is a small adapter over `httpx`, which
    is already pinned, rather than google-auth's `requests` transport, which
    would pull in an unpinned dependency for one POST an hour.
    """

    def __init__(self, credentials: Any) -> None:
        self._credentials = credentials
        self._lock = asyncio.Lock()

    async def __call__(self) -> str:
        async with self._lock:
            if not self._credentials.valid:
                await asyncio.to_thread(self._credentials.refresh, _HttpxGoogleAuthRequest())
            return str(self._credentials.token)

    def invalidate(self) -> None:
        """Force a refresh on next use — after FCM answers 401."""
        self._credentials.token = None


class FcmV1PushSender:
    """`PushSender` over the FCM HTTP v1 API.

    `transport` and `sleep` are injectable so tests drive the retry logic with
    an `httpx.MockTransport` and a no-op sleep — no network, no real waiting.
    A fresh `AsyncClient` per send keeps the sender free of event-loop-bound
    state: it is a long-lived singleton, and an httpx client bound to one loop
    fails on the next.
    """

    def __init__(
        self,
        project_id: str,
        token_provider: AccessTokenProvider,
        *,
        max_attempts: int = 3,
        base_delay_seconds: float = 1.0,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        if not project_id:
            raise ValueError("FCM project id is required")
        self._url = FCM_SEND_URL.format(project_id=project_id)
        self._token_provider = token_provider
        self._max_attempts = max(1, max_attempts)
        self._base_delay = max(0.0, base_delay_seconds)
        self._transport = transport
        self._sleep = sleep

    @staticmethod
    def build_payload(token: str, message: PushMessage) -> dict[str, Any]:
        """The v1 `messages:send` body, per TRD §8 "FCM Message Payload"."""
        return {
            "message": {
                "token": token,
                "notification": {"title": message.title, "body": message.body},
                "data": {str(k): str(v) for k, v in message.data.items()},
                # High priority: the TRD target is delivery within 60 seconds,
                # which normal priority does not promise on a dozing Android.
                "android": {"priority": "high"},
                "apns": {"headers": {"apns-priority": "10"}},
            }
        }

    async def send(self, token: str, message: PushMessage) -> PushResult:
        payload = self.build_payload(token, message)
        last_error = "no attempt made"

        for attempt in range(1, self._max_attempts + 1):
            retry_after: float | None = None
            try:
                access_token = await self._token_provider()
                async with httpx.AsyncClient(transport=self._transport, timeout=FCM_REQUEST_TIMEOUT_SECONDS) as client:
                    response = await client.post(
                        self._url,
                        json=payload,
                        headers={"Authorization": f"Bearer {access_token}"},
                    )
            except Exception as exc:
                # Network failure, or minting the access token failed. Both
                # are transient from here: nothing was decided about the token.
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if response.is_success:
                    return PushResult(outcome=PushOutcome.DELIVERED, attempts=attempt)

                status_code, fcm_status, error_code, error_message = _parse_error(response)
                last_error = f"HTTP {status_code} {error_code or fcm_status or ''} {error_message}".strip()

                if _is_dead_token(status_code, fcm_status, error_code, error_message):
                    return PushResult(outcome=PushOutcome.INVALID_TOKEN, attempts=attempt, error=last_error)

                if status_code == 401:
                    # The cached access token expired between `valid` and the
                    # send. Drop it so the next attempt mints a fresh one.
                    invalidate = getattr(self._token_provider, "invalidate", None)
                    if callable(invalidate):
                        invalidate()
                elif status_code not in _RETRYABLE_STATUS:
                    # A definite answer that retrying cannot change.
                    return PushResult(outcome=PushOutcome.FAILED, attempts=attempt, error=last_error)
                retry_after = _retry_after_seconds(response)

            if attempt < self._max_attempts:
                delay = self._base_delay * (2 ** (attempt - 1))
                if retry_after is not None:
                    delay = max(delay, retry_after)
                await self._sleep(min(delay, MAX_RETRY_DELAY_SECONDS))

        logger.warning(
            "FCM delivery failed after %d attempts token=%s: %s",
            self._max_attempts,
            redact_token(token),
            last_error,
        )
        return PushResult(outcome=PushOutcome.FAILED, attempts=self._max_attempts, error=last_error)


def _parse_error(response: httpx.Response) -> tuple[int, str | None, str | None, str]:
    """Pull `(http_status, status, FcmError.errorCode, message)` out of an FCM error body.

    Shape: `{"error": {"code": 404, "status": "NOT_FOUND", "message": "...",
    "details": [{"@type": "...FcmError", "errorCode": "UNREGISTERED"}]}}`.
    A body that is not that shape (a proxy's HTML 502) yields Nones, never an
    exception.
    """
    try:
        error = response.json().get("error") or {}
    except (ValueError, AttributeError):
        return response.status_code, None, None, ""
    if not isinstance(error, dict):
        return response.status_code, None, None, ""

    error_code = None
    for detail in error.get("details") or []:
        if isinstance(detail, dict) and detail.get("errorCode"):
            error_code = str(detail["errorCode"])
            break
    return response.status_code, error.get("status"), error_code, str(error.get("message") or "")


def _is_dead_token(status_code: int, fcm_status: str | None, error_code: str | None, message: str) -> bool:
    """Whether FCM has said this registration token will never work again.

    See the module docstring for why `INVALID_ARGUMENT` needs the message check.
    """
    if error_code == "UNREGISTERED" or status_code == 404:
        return True
    if "INVALID_ARGUMENT" in (error_code, fcm_status):
        return "registration token" in message.lower()
    return False


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """A numeric `Retry-After`, if FCM sent one (it may on 429 and 503)."""
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None  # HTTP-date form; fall back to exponential backoff


class _HttpxGoogleAuthRequest:
    """`google.auth.transport.Request` implemented over a synchronous httpx client.

    Only ever called from a worker thread (see `ServiceAccountTokenProvider`).
    """

    def __call__(
        self,
        url: str,
        method: str = "GET",
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        **_: Any,
    ) -> _HttpxGoogleAuthResponse:
        from google.auth import exceptions as google_exceptions

        try:
            with httpx.Client(timeout=timeout or FCM_REQUEST_TIMEOUT_SECONDS) as client:
                response = client.request(method, url, content=body, headers=dict(headers or {}))
        except httpx.HTTPError as exc:
            raise google_exceptions.TransportError(str(exc)) from exc
        return _HttpxGoogleAuthResponse(response)


class _HttpxGoogleAuthResponse:
    """`google.auth.transport.Response` view of an httpx response."""

    def __init__(self, response: httpx.Response) -> None:
        self.status = response.status_code
        self.headers = dict(response.headers)
        self.data = response.content


# ── Selection ───────────────────────────────────────────────────────────


def _load_service_account_info(raw: str) -> dict[str, Any] | None:
    """Parse `FCM_SERVICE_ACCOUNT_JSON`: inline JSON, a file path, or empty.

    Raises `ValueError` on a value that is set but unusable. A half-configured
    production credential must be loud, not quietly downgraded to logging.
    """
    raw = raw.strip()
    if not raw:
        return None
    text = raw if raw.startswith("{") else Path(raw).read_text(encoding="utf-8")
    info = json.loads(text)
    if not isinstance(info, dict) or info.get("type") != "service_account":
        raise ValueError("FCM_SERVICE_ACCOUNT_JSON is not a Google service-account key")
    return info


def build_push_sender() -> PushSender:
    """Choose the sender from settings. Raises if FCM is configured but broken."""
    info = _load_service_account_info(settings.FCM_SERVICE_ACCOUNT_JSON)
    if info is None:
        logger.warning("FCM is not configured (FCM_SERVICE_ACCOUNT_JSON is empty); push notifications are logged only.")
        return LoggingPushSender()

    from google.oauth2 import service_account

    project_id = settings.FCM_PROJECT_ID or str(info.get("project_id") or "")
    credentials = service_account.Credentials.from_service_account_info(info, scopes=[FCM_OAUTH_SCOPE])
    logger.info("FCM HTTP v1 push enabled for project %s", project_id)
    return FcmV1PushSender(
        project_id,
        ServiceAccountTokenProvider(credentials),
        max_attempts=settings.FCM_MAX_ATTEMPTS,
        base_delay_seconds=settings.FCM_RETRY_BASE_DELAY_SECONDS,
    )


@lru_cache(maxsize=1)
def get_push_sender() -> PushSender:
    """Process-wide sender, built on first use.

    A construction failure is *not* cached — `lru_cache` does not memoise an
    exception — so a broken credential is reported on every notification it
    blocks, rather than once at first use and then never again.
    """
    return build_push_sender()
