"""Integration tests for notifications, against a live database.

These replace `test_mock_server.py::TestNotificationsMock`, which pinned the
Phase 0 stub's static JSON.

What is being proven, and why it needs Postgres rather than a faked session:

* **The notification centre is scoped to its owner.** Another user's
  notification is a 404 on read — the same answer as an id that never existed
  — so the endpoint cannot be used to probe ids.
* **A status change notifies the reporter**, through a real FastAPI background
  task running after the response, on its own session.
* **Push failure never fails the status change**, and never stops the
  notification row being written — the in-app centre is the source of truth.
* **A token FCM calls dead is deleted**, and only that user's copy of it.

The push transport is replaced with a recording fake in every test here, so
nothing leaves the process. `app/core/push.py` has its own unit tests.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator

import pytest
from httpx import AsyncClient
from sqlalchemy import NullPool, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings
from app.core.push import PushMessage, PushOutcome, PushResult
from app.core.security import create_access_token
from app.models.fcm_token import FcmToken
from app.models.notification import Notification
from app.models.user import User
from app.services import notification_service

LAT, LNG = 12.9716, 77.5946


# ── Database gate (mirrors test_issues.py) ──────────────────────────────


async def _database_is_reachable() -> bool:
    engine = create_async_engine(settings.TEST_DATABASE_URL, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:  # every failure mode here means "no database reachable"
        return False
    else:
        return True
    finally:
        await engine.dispose()


@pytest.fixture(scope="module", autouse=True)
def _require_database() -> None:
    if not asyncio.run(_database_is_reachable()):
        pytest.skip(
            f"Test database {settings.TEST_DATABASE_URL} is unreachable; "
            "start Postgres (docker compose up -d db) to run the notification integration tests.",
        )


@pytest.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """One rolled-back transaction per test, on a loop-private engine (see test_issues.py)."""
    engine = create_async_engine(settings.TEST_DATABASE_URL, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            transaction = await conn.begin()
            session = AsyncSession(bind=conn, expire_on_commit=False)
            try:
                yield session
            finally:
                await session.close()
                await transaction.rollback()
    finally:
        await engine.dispose()


# ── Fake push transport ─────────────────────────────────────────────────


class RecordingSender:
    """A `PushSender` that records sends and answers from a per-token script."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, PushMessage]] = []
        self.outcomes: dict[str, PushResult] = {}
        self.raise_error: Exception | None = None

    async def send(self, token: str, message: PushMessage) -> PushResult:
        if self.raise_error is not None:
            raise self.raise_error
        self.sent.append((token, message))
        return self.outcomes.get(token, PushResult(outcome=PushOutcome.DELIVERED, attempts=1))


@pytest.fixture
def sender(monkeypatch: pytest.MonkeyPatch) -> RecordingSender:
    fake = RecordingSender()
    monkeypatch.setattr(notification_service, "get_push_sender", lambda: fake)
    return fake


# ── Helpers ─────────────────────────────────────────────────────────────


async def _user(db: AsyncSession, role: str = "CITIZEN", *, is_active: bool = True) -> User:
    user = User(
        email=f"{role.lower()}-{uuid.uuid4().hex[:12]}@example.com",
        name=f"Test {role.title()}",
        password_hash="x",
        role=role,
        is_anonymous=False,
        is_active=is_active,
    )
    db.add(user)
    await db.flush()
    return user


def _auth(user: User) -> dict[str, str]:
    token = create_access_token(user_id=str(user.id), role=user.role, email=user.email)
    return {"Authorization": f"Bearer {token}"}


async def _token(db: AsyncSession, user: User, value: str | None = None) -> str:
    value = value or f"fcm-{uuid.uuid4().hex}"
    db.add(FcmToken(user_id=user.id, device_token=value))
    await db.flush()
    return value


async def _notification(db: AsyncSession, user: User, *, is_read: bool = False, title: str = "Hello") -> Notification:
    row = Notification(user_id=user.id, type="SYSTEM", channel="IN_APP", title=title, body="Body", is_read=is_read)
    db.add(row)
    await db.flush()
    return row


async def _submit(client: AsyncClient, reporter: User | None, **overrides) -> dict:
    form = {"category": "POTHOLE", "latitude": str(LAT), "longitude": str(LNG), **overrides}
    response = await client.post("/v1/issues", data=form, headers=_auth(reporter) if reporter else {})
    assert response.status_code == 201, response.text
    return response.json()


async def _notifications_for(db: AsyncSession, user: User) -> list[Notification]:
    """Fresh from the database: the background task wrote them on its own session.

    `populate_existing` rather than `expire_all()` — expiring would also expire
    `user`, and reading its id would then lazy-load outside a greenlet.
    """
    stmt = (
        select(Notification)
        .where(Notification.user_id == user.id)
        .order_by(Notification.created_at)
        .execution_options(populate_existing=True)
    )
    return list((await db.scalars(stmt)).all())


async def _tokens_for(db: AsyncSession, user: User) -> list[str]:
    return list((await db.scalars(select(FcmToken.device_token).where(FcmToken.user_id == user.id))).all())


# ── GET /notifications ──────────────────────────────────────────────────


class TestList:
    async def test_requires_authentication(self, client: AsyncClient) -> None:
        assert (await client.get("/v1/notifications")).status_code == 401

    async def test_empty_list_for_a_new_user(self, client: AsyncClient, db_session: AsyncSession) -> None:
        user = await _user(db_session)
        response = await client.get("/v1/notifications", headers=_auth(user))

        assert response.status_code == 200
        assert response.json() == {
            "items": [],
            "total": 0,
            "page": 1,
            "page_size": 20,
            "total_pages": 1,
            "unread_count": 0,
        }

    async def test_lists_own_notifications_newest_first(self, client: AsyncClient, db_session: AsyncSession) -> None:
        user = await _user(db_session)
        first = await _notification(db_session, user, title="first")
        second = await _notification(db_session, user, title="second")

        body = (await client.get("/v1/notifications", headers=_auth(user))).json()

        assert [i["id"] for i in body["items"]] == [str(second.id), str(first.id)]
        item = body["items"][0]
        assert set(item) == {"id", "type", "channel", "title", "body", "issue_id", "is_read", "created_at"}
        assert item["is_read"] is False

    async def test_never_shows_another_users_notifications(self, client: AsyncClient, db_session: AsyncSession) -> None:
        me, other = await _user(db_session), await _user(db_session)
        await _notification(db_session, other)

        body = (await client.get("/v1/notifications", headers=_auth(me))).json()

        assert body["items"] == []
        assert body["unread_count"] == 0

    async def test_delivery_bookkeeping_is_not_exposed(self, client: AsyncClient, db_session: AsyncSession) -> None:
        user = await _user(db_session)
        await _notification(db_session, user)
        item = (await client.get("/v1/notifications", headers=_auth(user))).json()["items"][0]
        assert "sent_at" not in item
        assert "retry_count" not in item
        assert "user_id" not in item

    async def test_pagination_and_unread_count(self, client: AsyncClient, db_session: AsyncSession) -> None:
        user = await _user(db_session)
        for _ in range(3):
            await _notification(db_session, user)
        await _notification(db_session, user, is_read=True)

        body = (await client.get("/v1/notifications", params={"page_size": 2, "page": 2}, headers=_auth(user))).json()

        assert body["total"] == 4
        assert body["total_pages"] == 2
        assert len(body["items"]) == 2
        assert body["unread_count"] == 3

    async def test_unread_only_filter_keeps_the_global_badge(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        user = await _user(db_session)
        unread = await _notification(db_session, user)
        await _notification(db_session, user, is_read=True)

        body = (await client.get("/v1/notifications", params={"unread_only": True}, headers=_auth(user))).json()

        assert [i["id"] for i in body["items"]] == [str(unread.id)]
        assert body["total"] == 1
        assert body["unread_count"] == 1

    async def test_page_size_is_capped(self, client: AsyncClient, db_session: AsyncSession) -> None:
        user = await _user(db_session)
        response = await client.get("/v1/notifications", params={"page_size": 101}, headers=_auth(user))
        assert response.status_code == 422


# ── PATCH /notifications/{id}/read ──────────────────────────────────────


class TestMarkRead:
    async def test_marks_own_notification_read(self, client: AsyncClient, db_session: AsyncSession) -> None:
        user = await _user(db_session)
        row = await _notification(db_session, user)

        response = await client.patch(f"/v1/notifications/{row.id}/read", headers=_auth(user))

        assert response.status_code == 200, response.text
        assert response.json()["id"] == str(row.id)
        assert response.json()["is_read"] is True
        await db_session.refresh(row)
        assert row.is_read is True

    async def test_is_idempotent(self, client: AsyncClient, db_session: AsyncSession) -> None:
        user = await _user(db_session)
        row = await _notification(db_session, user, is_read=True)
        response = await client.patch(f"/v1/notifications/{row.id}/read", headers=_auth(user))
        assert response.status_code == 200
        assert response.json()["is_read"] is True

    async def test_someone_elses_notification_is_404_not_403(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        """Indistinguishable from a nonexistent id, so ids cannot be probed."""
        me, other = await _user(db_session), await _user(db_session)
        theirs = await _notification(db_session, other)

        foreign = await client.patch(f"/v1/notifications/{theirs.id}/read", headers=_auth(me))
        missing = await client.patch(f"/v1/notifications/{uuid.uuid4()}/read", headers=_auth(me))

        assert foreign.status_code == 404
        assert foreign.json() == missing.json()
        await db_session.refresh(theirs)
        assert theirs.is_read is False, "the other user's row must be untouched"

    async def test_malformed_id_is_422(self, client: AsyncClient, db_session: AsyncSession) -> None:
        user = await _user(db_session)
        assert (await client.patch("/v1/notifications/not-a-uuid/read", headers=_auth(user))).status_code == 422

    async def test_requires_authentication(self, client: AsyncClient) -> None:
        assert (await client.patch(f"/v1/notifications/{uuid.uuid4()}/read")).status_code == 401


# ── PATCH /notifications/read-all ───────────────────────────────────────


class TestMarkAllRead:
    async def test_marks_only_own_unread_rows(self, client: AsyncClient, db_session: AsyncSession) -> None:
        me, other = await _user(db_session), await _user(db_session)
        await _notification(db_session, me)
        await _notification(db_session, me)
        await _notification(db_session, me, is_read=True)
        theirs = await _notification(db_session, other)

        response = await client.patch("/v1/notifications/read-all", headers=_auth(me))

        assert response.status_code == 200
        assert response.json() == {"updated_count": 2}
        assert all(n.is_read for n in await _notifications_for(db_session, me))
        await db_session.refresh(theirs)
        assert theirs.is_read is False

    async def test_second_call_updates_nothing(self, client: AsyncClient, db_session: AsyncSession) -> None:
        user = await _user(db_session)
        await _notification(db_session, user)
        await client.patch("/v1/notifications/read-all", headers=_auth(user))
        response = await client.patch("/v1/notifications/read-all", headers=_auth(user))
        assert response.json() == {"updated_count": 0}

    async def test_route_is_not_shadowed_by_the_id_route(self, client: AsyncClient, db_session: AsyncSession) -> None:
        user = await _user(db_session)
        assert (await client.patch("/v1/notifications/read-all", headers=_auth(user))).status_code == 200


# ── Status change → reporter notification (background task) ─────────────


class TestStatusChangeNotification:
    async def test_reporter_is_notified_and_pushed(
        self, client: AsyncClient, db_session: AsyncSession, sender: RecordingSender
    ) -> None:
        reporter = await _user(db_session)
        token = await _token(db_session, reporter)
        issue = await _submit(client, reporter, address_text="MG Road")
        authority = await _user(db_session, role="AUTHORITY")

        response = await client.patch(
            f"/v1/issues/{issue['issue_id']}/status", json={"status": "IN_PROGRESS"}, headers=_auth(authority)
        )
        assert response.status_code == 200, response.text

        rows = await _notifications_for(db_session, reporter)
        assert len(rows) == 1
        row = rows[0]
        assert row.type == "STATUS_CHANGE"
        assert row.channel == "PUSH"
        assert row.issue_id == uuid.UUID(issue["issue_id"])
        assert row.title == f"Update on {issue['issue_number']}"
        assert row.body == "Your pothole report on MG Road is being addressed."
        assert row.sent_at is not None
        assert row.retry_count == 0

        assert len(sender.sent) == 1
        pushed_token, message = sender.sent[0]
        assert pushed_token == token
        assert message.data == {
            "issue_id": issue["issue_id"],
            "issue_number": issue["issue_number"],
            "new_status": "IN_PROGRESS",
            "deep_link": f"weft://issues/{issue['issue_id']}",
            "notification_id": str(row.id),
        }

    async def test_notification_appears_in_the_reporters_centre(
        self, client: AsyncClient, db_session: AsyncSession, sender: RecordingSender
    ) -> None:
        reporter = await _user(db_session)
        issue = await _submit(client, reporter)
        authority = await _user(db_session, role="AUTHORITY")
        await client.patch(
            f"/v1/issues/{issue['issue_id']}/status", json={"status": "RESOLVED"}, headers=_auth(authority)
        )

        body = (await client.get("/v1/notifications", headers=_auth(reporter))).json()

        assert body["unread_count"] == 1
        assert body["items"][0]["issue_id"] == issue["issue_id"]
        assert body["items"][0]["body"] == "Your pothole report has been resolved."

    @pytest.mark.parametrize(
        ("new_status", "expected"),
        [
            ("IN_PROGRESS", "Your water logging report is being addressed."),
            ("RESOLVED", "Your water logging report has been resolved."),
            ("REJECTED", "Your report was reviewed and could not be actioned."),
        ],
    )
    async def test_message_per_trd_trigger_table(
        self, client: AsyncClient, db_session: AsyncSession, sender: RecordingSender, new_status: str, expected: str
    ) -> None:
        reporter = await _user(db_session)
        issue = await _submit(client, reporter, category="WATER_LOGGING")
        authority = await _user(db_session, role="AUTHORITY")
        await client.patch(
            f"/v1/issues/{issue['issue_id']}/status", json={"status": new_status}, headers=_auth(authority)
        )
        assert (await _notifications_for(db_session, reporter))[0].body == expected

    async def test_without_a_device_the_row_is_in_app_only(
        self, client: AsyncClient, db_session: AsyncSession, sender: RecordingSender
    ) -> None:
        reporter = await _user(db_session)
        issue = await _submit(client, reporter)
        authority = await _user(db_session, role="AUTHORITY")
        await client.patch(
            f"/v1/issues/{issue['issue_id']}/status", json={"status": "IN_PROGRESS"}, headers=_auth(authority)
        )

        row = (await _notifications_for(db_session, reporter))[0]
        assert row.channel == "IN_APP"
        assert row.sent_at is None
        assert sender.sent == []

    async def test_anonymous_issue_notifies_nobody(
        self, client: AsyncClient, db_session: AsyncSession, sender: RecordingSender
    ) -> None:
        issue = await _submit(client, None)
        authority = await _user(db_session, role="AUTHORITY")

        response = await client.patch(
            f"/v1/issues/{issue['issue_id']}/status", json={"status": "IN_PROGRESS"}, headers=_auth(authority)
        )

        assert response.status_code == 200
        count = await db_session.scalar(
            select(Notification.id).where(Notification.issue_id == uuid.UUID(issue["issue_id"]))
        )
        assert count is None
        assert sender.sent == []

    async def test_actor_changing_their_own_report_is_not_notified(
        self, client: AsyncClient, db_session: AsyncSession, sender: RecordingSender
    ) -> None:
        authority = await _user(db_session, role="AUTHORITY")
        issue = await _submit(client, authority)
        await client.patch(
            f"/v1/issues/{issue['issue_id']}/status", json={"status": "IN_PROGRESS"}, headers=_auth(authority)
        )
        assert await _notifications_for(db_session, authority) == []

    async def test_illegal_transition_notifies_nobody(
        self, client: AsyncClient, db_session: AsyncSession, sender: RecordingSender
    ) -> None:
        reporter = await _user(db_session)
        issue = await _submit(client, reporter)
        authority = await _user(db_session, role="AUTHORITY")

        response = await client.patch(
            f"/v1/issues/{issue['issue_id']}/status", json={"status": "REPORTED"}, headers=_auth(authority)
        )

        assert response.status_code == 400
        assert await _notifications_for(db_session, reporter) == []

    async def test_deactivated_reporter_is_not_notified(
        self, client: AsyncClient, db_session: AsyncSession, sender: RecordingSender
    ) -> None:
        reporter = await _user(db_session)
        await _token(db_session, reporter)
        issue = await _submit(client, reporter)
        reporter.is_active = False
        await db_session.flush()
        authority = await _user(db_session, role="AUTHORITY")

        response = await client.patch(
            f"/v1/issues/{issue['issue_id']}/status", json={"status": "IN_PROGRESS"}, headers=_auth(authority)
        )

        assert response.status_code == 200
        assert await _notifications_for(db_session, reporter) == []
        assert sender.sent == []

    async def test_push_failure_does_not_fail_the_status_change(
        self, client: AsyncClient, db_session: AsyncSession, sender: RecordingSender
    ) -> None:
        """FCM down for all three attempts: the change stands and the row is kept."""
        reporter = await _user(db_session)
        token = await _token(db_session, reporter)
        sender.outcomes[token] = PushResult(outcome=PushOutcome.FAILED, attempts=3, error="HTTP 503")
        issue = await _submit(client, reporter)
        authority = await _user(db_session, role="AUTHORITY")

        response = await client.patch(
            f"/v1/issues/{issue['issue_id']}/status", json={"status": "RESOLVED"}, headers=_auth(authority)
        )

        assert response.status_code == 200
        assert response.json()["status"] == "RESOLVED"
        row = (await _notifications_for(db_session, reporter))[0]
        assert row.sent_at is None
        assert row.retry_count == 3
        assert await _tokens_for(db_session, reporter) == [token], "a transient failure keeps the token"

    async def test_a_sender_that_raises_cannot_fail_the_status_change(
        self, client: AsyncClient, db_session: AsyncSession, sender: RecordingSender
    ) -> None:
        reporter = await _user(db_session)
        await _token(db_session, reporter)
        sender.raise_error = RuntimeError("boom")
        issue = await _submit(client, reporter)
        authority = await _user(db_session, role="AUTHORITY")

        response = await client.patch(
            f"/v1/issues/{issue['issue_id']}/status", json={"status": "IN_PROGRESS"}, headers=_auth(authority)
        )

        assert response.status_code == 200
        assert len(await _notifications_for(db_session, reporter)) == 1

    async def test_unavailable_push_sender_cannot_fail_the_status_change(
        self, client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FCM configured but broken: the row is written, sent_at stays NULL."""

        def _broken() -> None:
            raise ValueError("FCM_SERVICE_ACCOUNT_JSON is not a Google service-account key")

        monkeypatch.setattr(notification_service, "get_push_sender", _broken)
        reporter = await _user(db_session)
        await _token(db_session, reporter)
        issue = await _submit(client, reporter)
        authority = await _user(db_session, role="AUTHORITY")

        response = await client.patch(
            f"/v1/issues/{issue['issue_id']}/status", json={"status": "IN_PROGRESS"}, headers=_auth(authority)
        )

        assert response.status_code == 200
        row = (await _notifications_for(db_session, reporter))[0]
        assert row.channel == "PUSH"
        assert row.sent_at is None


# ── deliver(): fan-out and dead-token cleanup ───────────────────────────


class TestDeliver:
    async def test_pushes_every_device_and_counts_failed_attempts(
        self, db_session: AsyncSession, sender: RecordingSender
    ) -> None:
        user = await _user(db_session)
        good, flaky = await _token(db_session, user), await _token(db_session, user)
        sender.outcomes[flaky] = PushResult(outcome=PushOutcome.DELIVERED, attempts=3)

        notification_id = await notification_service.deliver(
            db_session.bind, user_id=user.id, notification_type="SYSTEM", title="t", body="b"
        )

        assert {t for t, _ in sender.sent} == {good, flaky}
        row = await db_session.get(Notification, notification_id)
        await db_session.refresh(row)
        assert row.sent_at is not None
        assert row.retry_count == 2, "two failed tries before the flaky device accepted"

    async def test_dead_token_is_deleted_and_live_one_kept(
        self, db_session: AsyncSession, sender: RecordingSender
    ) -> None:
        user = await _user(db_session)
        live, dead = await _token(db_session, user), await _token(db_session, user)
        sender.outcomes[dead] = PushResult(outcome=PushOutcome.INVALID_TOKEN, attempts=1, error="UNREGISTERED")

        await notification_service.deliver(
            db_session.bind, user_id=user.id, notification_type="SYSTEM", title="t", body="b"
        )

        assert await _tokens_for(db_session, user) == [live]

    async def test_all_tokens_dead_leaves_the_row_unsent(
        self, db_session: AsyncSession, sender: RecordingSender
    ) -> None:
        user = await _user(db_session)
        dead = await _token(db_session, user)
        sender.outcomes[dead] = PushResult(outcome=PushOutcome.INVALID_TOKEN, attempts=1)

        notification_id = await notification_service.deliver(
            db_session.bind, user_id=user.id, notification_type="SYSTEM", title="t", body="b"
        )

        row = await db_session.get(Notification, notification_id)
        await db_session.refresh(row)
        assert row.sent_at is None
        assert await _tokens_for(db_session, user) == []

    async def test_unknown_user_records_nothing_and_does_not_raise(
        self, db_session: AsyncSession, sender: RecordingSender
    ) -> None:
        result = await notification_service.deliver(
            db_session.bind, user_id=uuid.uuid4(), notification_type="SYSTEM", title="t", body="b"
        )
        assert result is None
        assert sender.sent == []

    async def test_database_failure_does_not_raise(self, db_session: AsyncSession, sender: RecordingSender) -> None:
        """An invalid enum value fails the INSERT; deliver() must swallow and log it."""
        user = await _user(db_session)
        result = await notification_service.deliver(
            db_session.bind, user_id=user.id, notification_type="NOT_A_TYPE", title="t", body="b"
        )
        assert result is None
        # The caller's transaction survives: delivery ran in a savepoint.
        assert await db_session.get(User, user.id) is not None

    async def test_long_title_is_truncated_to_the_column(
        self, db_session: AsyncSession, sender: RecordingSender
    ) -> None:
        user = await _user(db_session)
        notification_id = await notification_service.deliver(
            db_session.bind, user_id=user.id, notification_type="SYSTEM", title="x" * 400, body="b"
        )
        row = await db_session.get(Notification, notification_id)
        assert len(row.title) == notification_service.TITLE_MAX_LENGTH


class TestMessageBuilder:
    def test_long_address_is_shortened(self) -> None:
        notice = notification_service.StatusChangeNotice(
            reporter_id=uuid.uuid4(),
            issue_id=uuid.uuid4(),
            issue_number="ISS-2026-ABC",
            category="BROKEN_STREET_LIGHT",
            address_text="A" * 300,
            new_status="RESOLVED",
        )
        _, body = notification_service.build_status_change_message(notice)
        assert body.startswith("Your broken street light report on AAA")
        assert len(body) < 140
        assert "…" in body
