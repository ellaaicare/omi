import asyncio
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from ella.routers import canonical_events, chat
from ella.routers.canonical_events import InMemoryCanonicalEventStore


def _turn_lookup_client(monkeypatch, store, *, uid="owner-a"):
    app = FastAPI()
    app.include_router(chat.router)
    app.dependency_overrides[chat.get_exact_firebase_uid] = lambda: uid
    monkeypatch.setattr(chat, "_canonical_event_store", store)
    return TestClient(app)


def _canonical_user_turn(uid, client_message_id, started_at):
    return chat._ios_chat_event(
        uid=uid,
        turn_id=client_message_id,
        role="user",
        text="synthetic test turn",
        session_key=f"ella:omi:{uid}:canonical",
        started_at=started_at,
    )


def test_exact_turn_lookup_finds_old_retained_turn_despite_timestamp_tie(monkeypatch):
    store = InMemoryCanonicalEventStore()
    tied_at = datetime(2020, 1, 1, 12, 0, tzinfo=timezone.utc)
    asyncio.run(
        store.write_batch(
            [
                _canonical_user_turn("owner-a", "retained-turn", tied_at),
                _canonical_user_turn("owner-a", "same-time-turn", tied_at),
            ]
        )
    )
    client = _turn_lookup_client(monkeypatch, store)

    response = client.get("/v1/ella/chat/turns/retained-turn")

    assert response.status_code == 200
    assert response.json() == {"exists": True}
    assert response.headers["cache-control"] == "private, no-store"


def test_exact_turn_lookup_uses_authenticated_owner_and_user_event_identity(monkeypatch):
    class RecordingStore:
        def __init__(self):
            self.calls = []

        async def event_exists(self, **kwargs):
            self.calls.append(kwargs)
            return True

    store = RecordingStore()
    client = _turn_lookup_client(monkeypatch, store, uid="owner-a")

    response = client.get("/v1/ella/chat/turns/client-123?uid=owner-b")

    assert response.status_code == 200
    assert response.json() == {"exists": True}
    assert store.calls == [
        {
            "uid": "owner-a",
            "event_id": "ios_chat:owner-a:client-123:user",
            "source_identity": "ios_chat:owner-a:client-123",
        }
    ]


def test_postgres_exact_turn_lookup_uses_content_free_indexed_predicate(monkeypatch):
    class RecordingPool:
        def __init__(self):
            self.calls = []

        async def fetchval(self, query, *args):
            self.calls.append((" ".join(query.split()), args))
            return True

    pool = RecordingPool()

    async def get_pool():
        return pool

    monkeypatch.setattr(canonical_events, "_get_pool", get_pool)
    store = canonical_events.PostgresCanonicalEventStore(
        reinterpretation_repository=object(),
        today_card_repository=object(),
    )

    exists = asyncio.run(
        store.event_exists(
            uid="owner-a",
            event_id="ios_chat:owner-a:client-123:user",
            source_identity="ios_chat:owner-a:client-123",
        )
    )

    assert exists is True
    assert len(pool.calls) == 1
    query, args = pool.calls[0]
    assert "SELECT EXISTS" in query
    assert "WHERE uid = $1 AND event_id = $2 AND source_identity = $3" in query
    assert args == (
        "owner-a",
        "ios_chat:owner-a:client-123:user",
        "ios_chat:owner-a:client-123",
    )
    assert "text" not in query.lower()


def test_exact_turn_lookup_does_not_expose_another_owners_same_client_id(monkeypatch):
    store = InMemoryCanonicalEventStore()
    asyncio.run(
        store.write_batch(
            [
                _canonical_user_turn(
                    "owner-a",
                    "shared-client-id",
                    datetime(2026, 9, 27, 8, 0, tzinfo=timezone.utc),
                )
            ]
        )
    )
    client = _turn_lookup_client(monkeypatch, store, uid="owner-b")

    response = client.get("/v1/ella/chat/turns/shared-client-id")

    assert response.status_code == 200
    assert response.json() == {"exists": False}


def test_exact_turn_lookup_storage_failure_is_retryable_not_absent(monkeypatch):
    class FailingStore:
        async def event_exists(self, **_kwargs):
            raise RuntimeError("synthetic storage failure")

    client = _turn_lookup_client(monkeypatch, FailingStore())

    response = client.get("/v1/ella/chat/turns/client-123")

    assert response.status_code == 503
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json() == {
        "detail": {
            "code": "canonical_turn_lookup_unavailable",
            "retryable": True,
        }
    }


def test_exact_turn_lookup_auth_failure_is_terminal_before_storage(monkeypatch):
    class RecordingStore:
        def __init__(self):
            self.calls = 0

        async def event_exists(self, **_kwargs):
            self.calls += 1
            return False

    def reject_auth():
        raise HTTPException(status_code=401, detail="Missing Firebase bearer token")

    store = RecordingStore()
    app = FastAPI()
    app.include_router(chat.router)
    app.dependency_overrides[chat.get_exact_firebase_uid] = reject_auth
    monkeypatch.setattr(chat, "_canonical_event_store", store)

    response = TestClient(app).get("/v1/ella/chat/turns/client-123")

    assert response.status_code == 401
    assert store.calls == 0


def test_temporal_chat_context_filters_morning_omi_fragments(monkeypatch):
    async def fake_fetch(uid, *, limit, before=None, channels=None, since=None, user_timezone=None):
        assert uid == "uid-1"
        assert channels == ["omi"]
        assert since
        return [
            {
                "event_id": "cafe",
                "channel": "omi",
                "title": "Cafe Visit - Ordering Food and Drinks",
                "text": (
                    "A full morning cafe visit with food and drink orders, including several specific items, "
                    "a longer exchange about the cafe, and enough surrounding detail to count as a meaningful "
                    "conversation rather than a one-word fragment."
                ),
                "started_at": "2026-05-11T17:49:30Z",
                "ended_at": "2026-05-11T18:05:00Z",
            },
            {
                "event_id": "brief",
                "channel": "omi",
                "title": "Brief Utterance",
                "text": "Okay.",
                "started_at": "2026-05-11T18:56:15Z",
                "ended_at": "2026-05-11T18:56:16Z",
                "metadata": {"ella_tags": ["omi", "low_signal"]},
            },
        ]

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return (
                datetime(2026, 5, 11, 20, 0, tzinfo=timezone.utc).astimezone(tz) if tz else datetime(2026, 5, 11, 20, 0)
            )

    monkeypatch.setattr(chat, "_fetch_chat_canonical_events", fake_fetch)
    monkeypatch.setattr(chat, "datetime", FixedDateTime)

    label, events = asyncio.run(chat._fetch_temporal_chat_context("uid-1", "what happened this morning?"))

    assert label == "same-day morning OMI context"
    assert [event["event_id"] for event in events] == ["cafe"]


def test_ios_chat_event_uses_stable_turn_identity():
    started_at = datetime(2026, 5, 27, 18, 30, tzinfo=timezone.utc)

    event = chat._ios_chat_event(
        uid="uid-1",
        turn_id="client-123",
        role="user",
        text="Remember the demo banana is on the blue shelf.",
        session_key="ella:omi:uid-1:canonical",
        started_at=started_at,
        client_info={"type": "ios-app"},
    )
    normalized = event.normalized()

    assert event.channel == "ios_chat"
    assert event.provider == "omi-ios-chat"
    assert event.role == "user"
    assert event.scan_policy == "immediate"
    assert event.event_id == "ios_chat:uid-1:client-123:user"
    assert normalized["source_identity"] == "ios_chat:uid-1:client-123"
    assert event.session_id == "ella:omi:uid-1:canonical"


def test_ios_chat_assistant_event_disables_scan_policy():
    started_at = datetime(2026, 5, 27, 18, 31, tzinfo=timezone.utc)

    event = chat._ios_chat_event(
        uid="uid-1",
        turn_id="client-123",
        role="assistant",
        text="I will remember that.",
        session_key="ella:omi:uid-1:canonical",
        started_at=started_at,
    )

    assert event.scan_policy == "none"
    assert event.event_id == "ios_chat:uid-1:client-123:assistant"


def test_missing_client_message_id_uses_per_message_fallback_identity():
    started_at = datetime(2026, 5, 27, 18, 31, tzinfo=timezone.utc)
    first = chat.EllaChatRequest(
        uid="uid-1",
        message="First message",
        conversation_id="conversation-a",
        client_sent_at="2026-05-27T18:31:00Z",
    )
    retry = chat.EllaChatRequest(
        uid="uid-1",
        message="First message",
        conversation_id="conversation-a",
        client_sent_at="2026-05-27T18:31:00Z",
    )
    second = chat.EllaChatRequest(
        uid="uid-1",
        message="Second message",
        conversation_id="conversation-a",
        client_sent_at="2026-05-27T18:31:01Z",
    )

    first_id = chat._canonical_turn_id("uid-1", first, started_at)
    assert chat._canonical_turn_id("uid-1", retry, started_at) == first_id
    assert chat._canonical_turn_id("uid-1", second, started_at) != first_id


def test_hermes_session_defaults_to_canonical(monkeypatch):
    monkeypatch.setattr(chat, "HERMES_CHAT_SESSION_SCOPE", "canonical")

    assert chat._hermes_chat_session_key("ABC123") == "ella:omi:abc123:canonical"
    assert chat._hermes_chat_session_key("User/123") == "ella:omi:user-123:canonical"
    assert chat._hermes_chat_memory_key("User/123") == "ella:omi:user-123:canonical"


def test_hermes_chat_headers_include_stable_session_key():
    headers = chat._hermes_chat_headers("ella:omi:abc123:ios-chat:daily-20260530", "ella:omi:abc123:canonical")

    assert headers["X-Hermes-Session-Id"] == "ella:omi:abc123:ios-chat:daily-20260530"
    assert headers["X-Hermes-Session-Key"] == "ella:omi:abc123:canonical"
    assert headers["Content-Type"] == "application/json"
