import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from ella.routers import chat
from ella.routers.canonical_events import InMemoryCanonicalEventStore
from utils.ella.canonical_context import canonical_events_to_server_messages


def _request(**overrides):
    started_at = datetime(2026, 8, 15, 20, 0, tzinfo=timezone.utc)
    values = {
        "uid": "uid-a",
        "session_id": "session-1",
        "turn_id": "turn-000001",
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


@pytest.mark.parametrize(
    "overrides",
    [
        {"user_transcript": ""},
        {"assistant_transcript": "  "},
        {"user_terminal": False},
        {"assistant_terminal": False},
        {"session_id": "cross/authority"},
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
    asyncio.run(chat.persist_v2v_voice_turn(_request(), authenticated_uid="uid-a"))

    events = asyncio.run(store.timeline(uid="uid-a", since=None, limit=50, channels=["ios_voice"]))
    refreshed = canonical_events_to_server_messages(events, limit=50)

    assert len(refreshed) == 2
    assert {message["sender"] for message in refreshed} == {"human", "ai"}
    assert {message["text"] for message in refreshed} == {"Durable user turn", "Durable assistant turn"}
