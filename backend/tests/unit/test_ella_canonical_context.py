import asyncio
import sys
import types

sys.modules.setdefault("python_multipart", types.SimpleNamespace(__version__="0.0.20"))

from ella.routers import chat as ella_chat
from ella.routers import guardian
from utils.ella.canonical_context import (
    canonical_events_to_server_messages,
    canonical_events_to_chat_turns,
    format_canonical_context,
)


def _cafe_event():
    return {
        "uid": "uid-1",
        "canonical_identity": "uid-1",
        "event_id": "omi:cafe-123:summary",
        "source_identity": "omi:cafe-123",
        "channel": "omi",
        "provider": "omi-backend",
        "role": "user",
        "text": "Cafe Coffee and Waffle Stop\n\nOrdered a noah drink and a waffle with oat.",
        "started_at": "2026-05-07T18:56:59.312831+00:00",
        "metadata": {"structured": {"title": "Cafe Coffee and Waffle Stop"}},
    }


def test_format_canonical_context_includes_latest_omi_summary():
    context = format_canonical_context([_cafe_event()])

    assert "Recent canonical timeline context" in context
    assert "Cafe Coffee and Waffle Stop" in context
    assert "waffle with oat" in context


def test_canonical_events_to_server_messages_maps_ios_history_shape():
    messages = canonical_events_to_server_messages([_cafe_event()])

    assert messages[0]["id"] == "omi:cafe-123:summary"
    assert messages[0]["sender"] == "human"
    assert messages[0]["metadata"]["source"] == "canonical_timeline"
    assert messages[0]["metadata"]["channel"] == "omi"
    assert "Cafe Coffee and Waffle Stop" in messages[0]["metadata"]["title"]


def test_chat_history_prefers_canonical_timeline(monkeypatch):
    async def fake_events(uid, *, limit, before=None):
        assert uid == "uid-1"
        assert limit == 5
        assert before is None
        return [_cafe_event()]

    async def fail_resolve(_uid):
        raise AssertionError("legacy routing fallback should not be used when canonical has events")

    monkeypatch.setattr(ella_chat, "_fetch_chat_canonical_events", fake_events)
    monkeypatch.setattr(ella_chat, "resolve_user_routing", fail_resolve)

    result = asyncio.run(ella_chat.ella_chat_history("uid-1", limit=5))

    assert result["source"] == "canonical_timeline"
    assert result["fallback"] is False
    assert result["messages"][0]["id"] == "omi:cafe-123:summary"


def test_guardian_recent_turns_prefers_canonical_timeline(monkeypatch):
    async def fake_timeline(uid, **kwargs):
        assert uid == "uid-1"
        return [_cafe_event()]

    async def fail_resolve(_uid):
        raise AssertionError("legacy routing fallback should not be used when canonical has events")

    monkeypatch.setattr(guardian, "fetch_canonical_timeline", fake_timeline)
    monkeypatch.setattr(guardian, "resolve_user_routing", fail_resolve)

    turns = asyncio.run(guardian._get_recent_chat_turns("uid-1", limit=3))

    assert turns == [{"role": "user", "content": _cafe_event()["text"]}]


def test_canonical_events_to_chat_turns_newest_first_for_guardian():
    first = {**_cafe_event(), "event_id": "first", "text": "first"}
    second = {**_cafe_event(), "event_id": "second", "text": "second", "role": "assistant"}

    turns = canonical_events_to_chat_turns([first, second], limit=2)

    assert turns == [
        {"role": "assistant", "content": "second"},
        {"role": "user", "content": "first"},
    ]
