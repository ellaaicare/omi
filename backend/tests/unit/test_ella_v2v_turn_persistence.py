import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from ella.routers import canonical_events, chat
from ella.routers.canonical_events import InMemoryCanonicalEventStore
from utils.ella.canonical_context import canonical_events_to_server_messages


class _ReadbackPool:
    def __init__(self):
        self.calls = []

    async def fetch(self, query, *args):
        self.calls.append((query, args))
        return []


def _request(**overrides):
    started_at = datetime(2026, 8, 15, 20, 0, tzinfo=timezone.utc)
    values = {
        "uid": "uid-a",
        "session_id": "session-1",
        "turn_id": "turn-000001",
        "user_event_id": "turn-000001:user",
        "assistant_event_id": "turn-000001:assistant",
        "user_transcript": "Durable user turn",
        "assistant_transcript": "Durable assistant turn",
        "user_terminal": True,
        "assistant_terminal": True,
        "started_at": started_at,
        "completed_at": started_at + timedelta(seconds=2),
    }
    values.update(overrides)
    return chat.EllaVoiceTurnRequest(**values)


def test_v2v_turn_write_is_idempotent_and_returns_existing_canonical_content(monkeypatch):
    store = InMemoryCanonicalEventStore()
    monkeypatch.setattr(chat, "_canonical_event_store", store)

    first = asyncio.run(chat.persist_v2v_voice_turn(_request(), authenticated_uid="uid-a"))
    replay = asyncio.run(
        chat.persist_v2v_voice_turn(
            _request(user_transcript="Conflicting retry", assistant_transcript="Conflicting reply"),
            authenticated_uid="uid-a",
        )
    )

    assert first["idempotent_replay"] is False
    assert replay["idempotent_replay"] is True
    assert {message["text"] for message in replay["messages"]} == {
        "Durable user turn",
        "Durable assistant turn",
    }
    assert len(store._events) == 2


def test_v2v_turn_returns_full_accepted_transcripts_without_history_truncation(monkeypatch):
    store = InMemoryCanonicalEventStore()
    monkeypatch.setattr(chat, "_canonical_event_store", store)
    long_user_text = "u" * 20000
    long_assistant_text = "a" * 20000

    result = asyncio.run(
        chat.persist_v2v_voice_turn(
            _request(user_transcript=long_user_text, assistant_transcript=long_assistant_text),
            authenticated_uid="uid-a",
        )
    )

    messages_by_sender = {message["sender"]: message["text"] for message in result["messages"]}
    assert messages_by_sender == {"human": long_user_text, "ai": long_assistant_text}


def test_v2v_turn_equal_timestamp_returns_user_before_assistant_with_stable_event_ids(monkeypatch):
    store = InMemoryCanonicalEventStore()
    monkeypatch.setattr(chat, "_canonical_event_store", store)
    timestamp = datetime(2026, 8, 15, 20, 0, tzinfo=timezone.utc)

    result = asyncio.run(
        chat.persist_v2v_voice_turn(
            _request(started_at=timestamp, completed_at=timestamp),
            authenticated_uid="uid-a",
        )
    )

    assert [(message["id"], message["sender"], message["created_at"]) for message in result["messages"]] == [
        ("turn-000001:user", "human", timestamp.isoformat()),
        ("turn-000001:assistant", "ai", timestamp.isoformat()),
    ]


def test_v2v_turn_readback_uses_leading_event_id_index_with_exact_owner_binding(monkeypatch):
    pool = _ReadbackPool()

    async def get_pool():
        return pool

    monkeypatch.setattr(canonical_events, "_get_pool", get_pool)
    store = canonical_events.PostgresCanonicalEventStore()
    event_ids = [
        "v2v-turn-00000000000000000000000000000001:user",
        "v2v-turn-00000000000000000000000000000001:assistant",
    ]

    result = asyncio.run(
        store.events_by_event_ids(
            uid="uid-a",
            source_identity="ios_voice:uid-a:session-1:turn-1",
            event_ids=event_ids,
        )
    )

    assert result == []
    assert len(pool.calls) == 1
    query, args = pool.calls[0]
    assert "event_id = ANY($1::text[])" in query
    assert "source_identity = $2" in query
    assert "uid = $3" in query
    assert "lower(uid)" not in query
    assert args == (event_ids, "ios_voice:uid-a:session-1:turn-1", "uid-a")


@pytest.mark.parametrize(
    "overrides",
    [
        {"user_transcript": ""},
        {"assistant_transcript": "  "},
        {"user_terminal": False},
        {"assistant_terminal": False},
        {"session_id": "cross/authority"},
        {"assistant_event_id": "turn-000001:user"},
    ],
)
def test_v2v_turn_rejects_partial_nonterminal_or_invalid_identity_without_writes(monkeypatch, overrides):
    store = InMemoryCanonicalEventStore()
    monkeypatch.setattr(chat, "_canonical_event_store", store)

    with pytest.raises(HTTPException) as raised:
        asyncio.run(chat.persist_v2v_voice_turn(_request(**overrides), authenticated_uid="uid-a"))

    assert raised.value.status_code == 422
    assert store._events == {}


def test_v2v_turn_rejects_cross_authority_before_canonical_write(monkeypatch):
    store = InMemoryCanonicalEventStore()
    monkeypatch.setattr(chat, "_canonical_event_store", store)

    with pytest.raises(HTTPException) as raised:
        asyncio.run(chat.persist_v2v_voice_turn(_request(uid="uid-b"), authenticated_uid="uid-a"))

    assert raised.value.status_code == 403
    assert store._events == {}


def test_v2v_turn_survives_canonical_history_refresh(monkeypatch):
    store = InMemoryCanonicalEventStore()
    monkeypatch.setattr(chat, "_canonical_event_store", store)
    long_user_text = "u" * 20000
    long_assistant_text = "a" * 20000
    asyncio.run(
        chat.persist_v2v_voice_turn(
            _request(user_transcript=long_user_text, assistant_transcript=long_assistant_text),
            authenticated_uid="uid-a",
        )
    )

    events = asyncio.run(store.timeline(uid="uid-a", since=None, limit=50, channels=["ios_voice"]))
    refreshed = canonical_events_to_server_messages(events, limit=50)

    assert len(refreshed) == 2
    assert {message["sender"] for message in refreshed} == {"human", "ai"}
    assert {message["text"] for message in refreshed} == {long_user_text, long_assistant_text}


def test_equal_timestamp_canonical_history_preserves_terminal_user_assistant_chronology(monkeypatch):
    store = InMemoryCanonicalEventStore()
    monkeypatch.setattr(chat, "_canonical_event_store", store)
    timestamp = datetime(2026, 8, 15, 20, 0, tzinfo=timezone.utc)
    asyncio.run(
        chat.persist_v2v_voice_turn(
            _request(started_at=timestamp, completed_at=timestamp),
            authenticated_uid="uid-a",
        )
    )

    events = asyncio.run(store.timeline(uid="uid-a", since=None, limit=50, channels=["ios_voice"]))
    refreshed = canonical_events_to_server_messages(events, limit=50)

    assert [message["id"] for message in refreshed] == ["turn-000001:user", "turn-000001:assistant"]
    assert [message["sender"] for message in refreshed] == ["human", "ai"]
