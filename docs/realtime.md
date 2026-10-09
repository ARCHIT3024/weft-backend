# Realtime dashboard feed

`WS /v1/ws/dashboard` pushes issue events to the authority dashboard as they
happen. This is the contract `weft-web` (task 3.11) is built against.
`openapi.yaml` cannot describe a WebSocket, so this document is the spec.

Code: `app/routers/websocket.py` (socket), `app/services/realtime_service.py`
(what each event says and who hears it), `app/core/events.py` (envelope,
channels, publish). Tests: `tests/integration/test_realtime.py`,
`tests/unit/test_realtime_events.py`.

> **Where this departs from TRD §5.** The TRD puts the token in `?token=`, closes
> unauthenticated sockets with `4001`, and uses an `{event, timestamp, data}`
> envelope. This implementation sends the token in the first message (a URL
> ends up in proxy and access logs), uses the close codes below, and uses the
> envelope below. The TRD is stale on these points.

## The feed is a hint, not a ledger

Redis pub/sub delivers to whoever is subscribed **at that moment** and keeps
nothing. An event published while a dashboard is disconnected, or during a
Redis outage, is gone. The REST API is the source of truth, so the client rule
is simple:

> **On every `ready`, refetch what is on screen.** After that, apply events
> incrementally.

`ready` is only sent once the server's Redis subscription is confirmed, so
nothing published after `ready` can be missed by a connected socket.

## Connecting

```
wss://api.weft.city/v1/ws/dashboard
```

No query string, no subprotocol. The server accepts the socket, then waits for
one message:

```json
{"type": "auth", "token": "<access token>"}
```

Send it immediately after `onopen`. It must arrive within **5 seconds**, as a
text frame holding a JSON object. Nothing is sent and nothing is subscribed
before it. Use the same access token as the REST API. The dashboard keeps it
in memory (D-9), and that works unchanged here.

On success the server replies:

```json
{
  "type": "ready",
  "user_id": "3f0c…",
  "role": "AUTHORITY",
  "all_zones": false,
  "zone_ids": ["9a1e…", "c47d…"],
  "expires_at": "2026-10-10T09:32:11Z",
  "heartbeat_interval_seconds": 25.0
}
```

| Field | Meaning |
|---|---|
| `role` | `AUTHORITY` or `ADMIN`, read from the database, not the token. |
| `all_zones` | `true` for an admin. An admin hears every zone, including issues with no zone. |
| `zone_ids` | The authority's zones (`authority_zones`). Empty for an admin, and empty for an authority with no zones assigned. That authority stays connected but hears nothing, so show "no zones assigned" in the UI. |
| `expires_at` | The access token's `exp`. The server closes the socket at this instant. |
| `heartbeat_interval_seconds` | How often the server pings. |

### Who hears what

| Subscriber | Channels |
|---|---|
| AUTHORITY | `ws:zone:{zone_id}` for each of their zones |
| ADMIN | pattern `ws:zone:*`, which covers every zone **and** `ws:zone:none` |
| CITIZEN | refused (`4403`) |

An issue whose location is outside every zone is published to `ws:zone:none`.
Only admins receive it, which matches the REST rule that an unzoned issue is
admin-only to triage.

## Close codes

| Code | Reason string | When | Client should |
|---|---|---|---|
| `4400` | `expected_auth_message` | First message was not `{"type":"auth","token":"<non-empty string>"}` | Fix the client. Do not retry blindly. |
| `4401` | `unauthorized` | Token invalid, expired, missing `exp`, or the account is missing/deactivated | Refresh the access token, then reconnect. If refresh fails, log out. |
| `4401` | `token_expired` | The token reached `exp` while connected | Refresh, then reconnect. |
| `4401` | `account_deactivated` | Account deactivated while connected (checked every 60s) | Refresh, which will fail, then log out. |
| `4403` | `forbidden` | Account is not AUTHORITY/ADMIN (at connect, or demoted while connected) | Stop. Do not reconnect. |
| `4408` | `auth_timeout` | No auth message within 5s | Reconnect, and send auth on open. |
| `4409` | `subscription_changed` | The user's zones or role changed while connected | Reconnect immediately with the same token. |
| `1013` | `realtime_unavailable` / `revalidation_failed` | Redis or the database is unavailable | Reconnect with backoff (1s, 2s, 4s… capped at 30s). The REST API may still work. |
| `1011` | `internal_error` | Unexpected server error during auth | Reconnect with backoff. |
| `1006` | (none) | Network drop, no close frame | Reconnect with backoff. |

So a client can treat every `44xx` except `4400`/`4403` as "refresh the token
(if needed) and reconnect", and every `10xx` as "back off and reconnect".

## Token expiry

A socket never outlives its token. It is closed with `4401 token_expired` at
`exp`, and there is no in-band re-auth message. The client refreshes through
`POST /v1/auth/refresh` and opens a new socket. Access tokens last 24 hours,
so this happens about once a day. To avoid even that gap, the client may
refresh shortly before `expires_at` and reconnect pre-emptively.

The account is also re-read every 60 seconds, the same `users.is_active`
check `get_current_user` makes on every REST call. Deactivation closes the
socket with `4401`. A role or zone change closes it with `4403`/`4409`. A
deactivated authority therefore stops receiving events within a minute, not
when their token runs out.

## Heartbeat

The server sends this every 25 seconds, under the 60s idle timeout of an AWS
ALB and most proxies:

```json
{"type": "ping", "sent_at": "2026-10-09T09:32:36Z"}
```

No reply is required. If nothing at all (event or ping) arrives for about
**60 seconds**, treat the connection as dead: close it and reconnect.

The client may send `{"type": "ping"}` at any time and gets `{"type": "pong"}`
back. Any other client message is ignored, so a newer client does not break
an older server.

## Event envelope

Every event has exactly these top-level keys:

```json
{
  "type": "issue.created",
  "event_id": "5d7b2c1e-…",
  "issue_id": "550e8400-e29b-41d4-a716-446655440000",
  "zone_id": "9a1e…",
  "department_id": "e2b4…",
  "occurred_at": "2026-10-09T09:32:11.482913Z",
  "data": {
    "issue": { "…IssueSummary…" }
  }
}
```

| Field | Notes |
|---|---|
| `type` | One of the four below. Ignore types you do not know; more will be added (e.g. `issue.sla_breach`, task 4.9). |
| `event_id` | Unique per event. Use it to drop duplicates if two sockets ever overlap across a reconnect. |
| `zone_id` | `null` for an issue outside every zone (admins only). |
| `department_id` | `null` only if routing found no department. |
| `occurred_at` | UTC, `Z` suffix, same format as the REST API. |
| `data.issue` | **Always an `IssueSummary`, field for field.** It is the same shape as an item of `GET /v1/issues`, and `Issue` in `weft-web/src/types/issue.ts`. Upsert it into the map/list by `id`; no refetch needed. |

`data.issue` fields: `id`, `issue_number`, `category`, `description`, `status`,
`latitude`, `longitude`, `address_text`, `upvote_count`, `zone_id`,
`department_id`, `assigned_to_id`, `resolved_at`, `created_at`, `updated_at`.
Photos and status history are not included; fetch `GET /v1/issues/{id}` for
those.

`description` and `address_text` are citizen-supplied free text. Render them as
text, never as HTML. This is the same stored-XSS concern as D-9.

## Event catalogue

All four are published **after the database transaction commits**, as FastAPI
background tasks. A request that fails or rolls back publishes nothing. All
four **fail open**: if Redis is down, the submission, status change,
assignment or upvote still succeeds, and the event is lost and logged.

### `issue.created`

Fired by `POST /v1/issues`, for anonymous and authenticated submissions alike.

```json
"data": { "issue": { "…IssueSummary, status REPORTED, upvote_count 0…" } }
```

Dashboard: add a marker.

### `issue.status_changed`

Fired by `PATCH /v1/issues/{id}/status` when a transition is accepted. A
refused transition (`400`) publishes nothing.

```json
"data": {
  "issue": { "…IssueSummary with the new status…" },
  "previous_status": "REPORTED",
  "new_status": "IN_PROGRESS",
  "changed_by_id": "<users.id of the actor>",
  "note": "Crew dispatched"
}
```

`note` may be `null`. Dashboard: update the marker.

### `issue.assigned`

Fired by `PATCH /v1/issues/{id}/assign`.

```json
"data": {
  "issue": { "…IssueSummary…" },
  "assigned_to_id": "<authority_users.id>",
  "assigned_at": "2026-10-09T09:40:02.118Z",
  "assigned_by_id": "<users.id of the actor>"
}
```

Note the two id spaces. `assigned_to_id` is an `authority_users.id`, as in the
REST API, while `assigned_by_id` is a `users.id`.

### `issue.high_upvote_alert`

Fired by `POST /v1/issues/{id}/upvote` when the issue's `upvote_count`
reaches its department's `upvote_alert_threshold` (D-1, default 10).

```json
"data": {
  "issue": { "…IssueSummary…" },
  "upvote_count": 10,
  "threshold": 10
}
```

Dashboard: show a toast.

**It fires once per issue.** Two mechanisms guarantee that:

1. It is only considered on the upvote that makes the count **equal** to the
   threshold, not on every upvote at or above it. The `trg_upvote_count`
   trigger serialises votes on the issue row, so exactly one committed upvote
   sees that value.
2. Before publishing, the server takes a Redis `SET NX` marker for the issue
   (`events:high_upvote_alert:{issue_id}`, 180-day TTL). Withdrawing and
   re-casting a vote at the boundary returns the count to the threshold
   again, but the marker is taken, so nothing is re-sent.

Accepted consequences:
- An issue with no department never alerts.
- Lowering a threshold below an issue's current count does not alert
  retroactively.
- If Redis is down at the crossing moment, the alert is lost. The issue's
  `upvote_count` still shows on the dashboard.

Ordinary upvote count changes are **not** published (the TRD's
`issue.upvote_updated` is not implemented). The count in any later event's
`data.issue`, or a refetch, brings it up to date.

## Scaling and operations

- **Multiple API instances.** Every socket holds its own Redis subscription,
  and every instance publishes to Redis. An event from any instance reaches
  every dashboard on every instance, with no sticky sessions required.
- **Connections.** Each open socket holds one Redis pub/sub connection from
  the shared pool. It holds a database connection only for a moment at auth
  and once a minute after that. Disconnects, server-initiated closes and
  shutdown all release the pub/sub connection.
- **Origin.** Authentication is an explicit token, not a cookie, so the socket
  does not check `Origin`. A cross-site page cannot ride the user's session.
- **Payload size.** One `IssueSummary` per event, at most about 1.5 KB.
