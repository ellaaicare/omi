import asyncio
import sys
import types

sys.modules.setdefault("python_multipart", types.SimpleNamespace(__version__="0.0.20"))

from ella.routers import chat as ella_chat
from ella.routers import guardian
from utils.ella.canonical_context import (
    DEFAULT_CONTEXT_CHANNELS,
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
    context = format_canonical_context([_cafe_event()], user_timezone="America/Los_Angeles")

    assert "Recent canonical timeline context" in context
    assert "Current user-local time" in context
    assert "America/Los_Angeles" in context
    assert "2026-05-07T11:56:59" in context
    assert "Cafe Coffee and Waffle Stop" in context
    assert "waffle with oat" in context


def test_default_context_channels_include_external_continuity_sources():
    assert "observer_memory" in DEFAULT_CONTEXT_CHANNELS
    assert "companion_observation" in DEFAULT_CONTEXT_CHANNELS
    assert "grok_conversation" in DEFAULT_CONTEXT_CHANNELS
    assert "companion_note" in DEFAULT_CONTEXT_CHANNELS


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

    result = asyncio.run(ella_chat.ella_chat_history("uid-1", limit=5, authenticated_uid="uid-1"))

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


def test_guardian_isolated_context_never_uses_openclaw_history(monkeypatch):
    async def no_events(uid, **kwargs):
        assert uid == "uid-isolated"
        return []

    async def fail_resolve(_uid):
        raise AssertionError("isolated Guardian context must not use OpenClaw history")

    monkeypatch.setattr(guardian, "fetch_canonical_timeline", no_events)
    monkeypatch.setattr(guardian, "runtime_bindings_enabled", lambda uid=None: True)
    monkeypatch.setattr(guardian, "resolve_user_routing", fail_resolve)

    assert asyncio.run(guardian._get_recent_chat_turns("uid-isolated", limit=3)) == []


def test_canonical_events_to_chat_turns_newest_first_for_guardian():
    first = {**_cafe_event(), "event_id": "first", "text": "first"}
    second = {**_cafe_event(), "event_id": "second", "text": "second", "role": "assistant"}

    turns = canonical_events_to_chat_turns([first, second], limit=2)

    assert turns == [
        {"role": "assistant", "content": "second"},
        {"role": "user", "content": "first"},
    ]
