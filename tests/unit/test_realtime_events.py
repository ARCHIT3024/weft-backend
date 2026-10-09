"""Unit tests for the realtime publisher and the pieces of the socket protocol
that need neither a database nor Redis.

The end-to-end behaviour — a real socket, real pub/sub, real commits — is in
`tests/integration/test_realtime.py`. What is pinned here is the contract the
React dashboard is built against: the envelope's exact keys, the channel
names, and the fail-open promise that a broken Redis never raises into a
request.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.core import events
from app.core.events import DomainEvent, EventType
from app.routers import websocket as ws
from app.schemas.issue import IssueSummary
from app.services import realtime_service
from app.services.realtime_service import Subscriber

ENVELOPE_KEYS = {"type", "event_id", "issue_id", "zone_id", "department_id", "occurred_at", "data"}


def _summary(**overrides: Any) -> IssueSummary:
    now = datetime.now(UTC)
    fields: dict[str, Any] = {
        "id": uuid.uuid4(),
        "issue_number": "ISS-2026-ABCDEFGHJK",
        "category": "POTHOLE",
        "description": "Deep pothole",
        "status": "REPORTED",
        "latitude": 12.9716,
        "longitude": 77.5946,
        "address_text": None,
        "upvote_count": 0,
        "zone_id": uuid.uuid4(),
        "department_id": uuid.uuid4(),
        "assigned_to_id": None,
        "resolved_at": None,
        "created_at": now,
        "updated_at": now,
        **overrides,
    }
    return IssueSummary(**fields)


class _RecordingRedis:
    """Just enough of `redis.asyncio.Redis` for the publisher."""

    def __init__(self, *, set_result: Any = True) -> None:
        self.published: list[tuple[str, str]] = []
        self.sets: list[tuple[str, dict[str, Any]]] = []
        self._set_result = set_result

    async def publish(self, channel: str, message: str) -> int:
        self.published.append((channel, message))
        return 1

    async def set(self, key: str, value: str, **kwargs: Any) -> Any:
        self.sets.append((key, kwargs))
        return self._set_result


class _BrokenRedis:
    async def publish(self, channel: str, message: str) -> int:
        raise ConnectionError("redis is down")

    async def set(self, key: str, value: str, **kwargs: Any) -> Any:
        raise ConnectionError("redis is down")


# ── Channels ────────────────────────────────────────────────────────────


class TestChannels:
    def test_zone_channel_follows_the_trd_convention(self) -> None:
        zone_id = uuid.uuid4()
        assert events.zone_channel(zone_id) == f"ws:zone:{zone_id}"

    def test_an_issue_with_no_zone_goes_to_the_named_admin_channel(self) -> None:
        assert events.zone_channel(None) == "ws:zone:none"

    def test_the_admin_pattern_covers_zones_and_the_no_zone_channel(self) -> None:
        import fnmatch

        pattern = events.all_zones_pattern()
        assert fnmatch.fnmatchcase(events.zone_channel(uuid.uuid4()), pattern)
        assert fnmatch.fnmatchcase(events.zone_channel(None), pattern)

    def test_the_prefix_is_read_at_call_time(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tests rely on this to move the feed onto a private prefix."""
        monkeypatch.setattr(events, "CHANNEL_PREFIX", "private:")
        assert events.zone_channel(None) == "private:none"
        assert events.all_zones_pattern() == "private:*"


# ── Envelope ────────────────────────────────────────────────────────────


class TestEnvelope:
    def test_envelope_has_exactly_the_documented_keys(self) -> None:
        issue = _summary()
        event = realtime_service._event(EventType.ISSUE_CREATED, issue)
        body = json.loads(event.model_dump_json())

        assert set(body) == ENVELOPE_KEYS
        assert body["type"] == "issue.created"
        assert body["issue_id"] == str(issue.id)
        assert body["zone_id"] == str(issue.zone_id)
        assert body["department_id"] == str(issue.department_id)

    def test_occurred_at_is_utc_with_a_z_suffix_like_the_rest_api(self) -> None:
        body = json.loads(realtime_service._event(EventType.ISSUE_CREATED, _summary()).model_dump_json())
        assert body["occurred_at"].endswith("Z")

    def test_data_issue_is_an_issue_summary_field_for_field(self) -> None:
        issue = _summary()
        body = json.loads(realtime_service._event(EventType.ISSUE_CREATED, issue).model_dump_json())
        assert set(body["data"]["issue"]) == set(IssueSummary.model_fields)
        assert IssueSummary.model_validate(body["data"]["issue"]) == issue

    def test_a_subclass_contributes_only_the_summary_fields(self) -> None:
        """The triage routes hand over an `IssueDetail`; photos and history stay out."""

        class _Wider(IssueSummary):
            images: list[str]

        wide = _Wider(**_summary().model_dump(), images=["photo.jpg"])
        body = json.loads(realtime_service._event(EventType.ISSUE_ASSIGNED, wide).model_dump_json())
        assert "images" not in body["data"]["issue"]

    def test_no_zone_serialises_as_null(self) -> None:
        body = json.loads(realtime_service._event(EventType.ISSUE_CREATED, _summary(zone_id=None)).model_dump_json())
        assert body["zone_id"] is None
        assert body["data"]["issue"]["zone_id"] is None

    def test_every_event_gets_its_own_id(self) -> None:
        issue = _summary()
        first = realtime_service._event(EventType.ISSUE_CREATED, issue)
        second = realtime_service._event(EventType.ISSUE_CREATED, issue)
        assert first.event_id != second.event_id

    def test_event_names_are_the_documented_catalogue(self) -> None:
        assert {e.value for e in EventType} == {
            "issue.created",
            "issue.status_changed",
            "issue.assigned",
            "issue.high_upvote_alert",
        }


# ── publish_event: fail open ────────────────────────────────────────────


def _event(zone_id: uuid.UUID | None = None) -> DomainEvent:
    return realtime_service._event(EventType.ISSUE_CREATED, _summary(zone_id=zone_id))


class TestPublishEvent:
    async def test_publishes_the_json_envelope_to_the_zone_channel(self) -> None:
        redis = _RecordingRedis()
        zone_id = uuid.uuid4()
        event = _event(zone_id)

        assert await events.publish_event(redis, event) is True  # type: ignore[arg-type]
        [(channel, message)] = redis.published
        assert channel == f"ws:zone:{zone_id}"
        assert json.loads(message)["event_id"] == str(event.event_id)

    async def test_no_zone_publishes_to_the_admin_channel(self) -> None:
        redis = _RecordingRedis()
        await events.publish_event(redis, _event(None))  # type: ignore[arg-type]
        assert redis.published[0][0] == "ws:zone:none"

    async def test_no_redis_client_is_a_logged_no_op(self) -> None:
        assert await events.publish_event(None, _event()) is False

    async def test_a_redis_failure_never_raises(self) -> None:
        """D-8: a cache outage must not become an outage of the action being announced."""
        assert await events.publish_event(_BrokenRedis(), _event()) is False  # type: ignore[arg-type]


# ── High-upvote alert marker ────────────────────────────────────────────


class TestHighUpvoteAlertMarker:
    def _alert(self) -> DomainEvent:
        return realtime_service._event(EventType.ISSUE_HIGH_UPVOTE_ALERT, _summary(), upvote_count=10, threshold=10)

    async def test_first_crossing_takes_the_marker_and_publishes(self) -> None:
        redis = _RecordingRedis(set_result=True)
        event = self._alert()

        assert await realtime_service.publish_high_upvote_alert(redis, event) is True  # type: ignore[arg-type]
        [(key, kwargs)] = redis.sets
        assert key == events.alert_marker_key(event.issue_id)
        assert kwargs == {"nx": True, "ex": events.ALERT_MARKER_TTL_SECONDS}
        assert len(redis.published) == 1

    async def test_a_taken_marker_suppresses_the_repeat(self) -> None:
        redis = _RecordingRedis(set_result=None)  # SET NX on an existing key
        assert await realtime_service.publish_high_upvote_alert(redis, self._alert()) is False  # type: ignore[arg-type]
        assert redis.published == []

    async def test_redis_down_drops_the_alert_without_raising(self) -> None:
        assert await realtime_service.publish_high_upvote_alert(_BrokenRedis(), self._alert()) is False  # type: ignore[arg-type]

    async def test_no_redis_client_drops_the_alert(self) -> None:
        assert await realtime_service.publish_high_upvote_alert(None, self._alert()) is False


# ── Subscriber scope ────────────────────────────────────────────────────


def _subscriber(**overrides: Any) -> Subscriber:
    fields: dict[str, Any] = {
        "user_id": uuid.uuid4(),
        "role": "AUTHORITY",
        "zone_ids": frozenset({uuid.uuid4(), uuid.uuid4()}),
        "all_zones": False,
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
        **overrides,
    }
    return Subscriber(**fields)


class TestSubscriber:
    def test_an_authority_subscribes_to_exactly_its_zones(self) -> None:
        sub = _subscriber()
        assert sub.channels == sorted(f"ws:zone:{z}" for z in sub.zone_ids)
        assert sub.patterns == []

    def test_an_authority_never_hears_the_no_zone_channel(self) -> None:
        assert "ws:zone:none" not in _subscriber().channels

    def test_an_admin_pattern_subscribes_to_everything(self) -> None:
        sub = _subscriber(role="ADMIN", zone_ids=frozenset(), all_zones=True)
        assert sub.channels == []
        assert sub.patterns == ["ws:zone:*"]

    def test_an_authority_with_no_zones_subscribes_to_nothing(self) -> None:
        sub = _subscriber(zone_ids=frozenset())
        assert sub.channels == [] and sub.patterns == []

    def test_scope_comparison_ignores_expiry_but_not_zones(self) -> None:
        sub = _subscriber()
        later = Subscriber(
            user_id=sub.user_id,
            role=sub.role,
            zone_ids=sub.zone_ids,
            all_zones=False,
            expires_at=sub.expires_at + timedelta(hours=1),
        )
        assert sub.same_scope_as(later)
        assert not sub.same_scope_as(_subscriber(user_id=sub.user_id))


# ── Socket protocol helpers ─────────────────────────────────────────────


class TestProtocolHelpers:
    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            ({"type": "websocket.receive", "text": '{"type": "ping"}'}, {"type": "ping"}),
            ({"type": "websocket.receive", "text": "not json"}, None),
            ({"type": "websocket.receive", "text": "[1, 2]"}, None),
            ({"type": "websocket.receive", "bytes": b'{"type": "ping"}'}, None),
        ],
    )
    def test_client_messages_must_be_json_objects_in_text_frames(
        self, message: dict[str, Any], expected: dict[str, Any] | None
    ) -> None:
        assert ws._parse_client_message(message) == expected

    def test_close_codes_are_the_documented_ones(self) -> None:
        assert {c.name: c.value for c in ws.CloseCode} == {
            "INTERNAL_ERROR": 1011,
            "TRY_AGAIN_LATER": 1013,
            "PROTOCOL_ERROR": 4400,
            "UNAUTHORIZED": 4401,
            "FORBIDDEN": 4403,
            "AUTH_TIMEOUT": 4408,
            "SUBSCRIPTION_CHANGED": 4409,
        }

    def test_the_heartbeat_beats_common_proxy_idle_timeouts(self) -> None:
        """A default AWS ALB drops a connection idle for 60 seconds."""
        assert ws.HEARTBEAT_INTERVAL_SECONDS < 60
