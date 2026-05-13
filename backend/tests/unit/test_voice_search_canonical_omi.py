import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import types
from typing import Optional
from zoneinfo import ZoneInfo


def _load_voice_omi_helpers(fetch_canonical_timeline):
    voice_py = Path(__file__).resolve().parents[2] / "ella" / "routers" / "voice.py"
    source = voice_py.read_text()
    start = source.index("def _keyword_score(")
    end = source.index("\n\nasync def _search_omi_conversations(", start)
    module = types.ModuleType("voice_omi_canonical_test")
    module.datetime = datetime
    module.timedelta = timedelta
    module.timezone = timezone
    module.ZoneInfo = ZoneInfo
    module.Optional = Optional
    module.fetch_canonical_timeline = fetch_canonical_timeline
    module.logger = types.SimpleNamespace(warning=lambda *_args, **_kwargs: None, info=lambda *_args, **_kwargs: None)
    exec(source[start:end], module.__dict__)
    return module


def _cafe_event():
    return {
        "event_id": "omi:cafe-123:summary",
        "source_identity": "omi:cafe-123",
        "channel": "omi",
        "provider": "omi-backend",
        "role": "user",
        "text": "Cafe Coffee and Waffle Stop\n\nOrdered a noah drink and a waffle with oat.",
        "started_at": "2026-05-07T18:56:59.312831+00:00",
        "metadata": {
            "structured": {
                "title": "Cafe Coffee and Waffle Stop",
                "emoji": "coffee",
                "category": "food",
            }
        },
    }


def test_search_canonical_omi_events_uses_timeline_channel_omi_first():
    calls = []

    async def fake_fetch(uid, **kwargs):
        calls.append((uid, kwargs))
        return [_cafe_event()]

    module = _load_voice_omi_helpers(fake_fetch)

    results = asyncio.run(
        module._search_canonical_omi_events(
            "5aGC5YE9BnhcSoTxxtT4ar6ILQy2",
            "Cafe Coffee and Waffle Stop",
            5,
            True,
        )
    )

    assert calls == [("5aGC5YE9BnhcSoTxxtT4ar6ILQy2", {"limit": 120, "channels": ["omi"]})]
    assert results[0]["source"] == "omi"
    assert results[0]["title"] == "Cafe Coffee and Waffle Stop"
    assert "waffle with oat" in results[0]["content"]
    assert results[0]["metadata"]["provenance"] == "canonical_event"
    assert results[0]["metadata"]["fallback"] is False
    assert results[0]["metadata"]["event_id"] == "omi:cafe-123:summary"


def test_search_canonical_omi_events_applies_this_morning_window():
    calls = []
    afternoon_event = {
        **_cafe_event(),
        "event_id": "omi:afternoon:summary",
        "source_identity": "omi:afternoon",
        "text": "Afternoon drive\n\nTalked about traffic after lunch.",
        "started_at": "2026-05-07T21:30:00+00:00",
        "metadata": {"structured": {"title": "Afternoon drive"}},
    }

    async def fake_fetch(uid, **kwargs):
        calls.append((uid, kwargs))
        return [afternoon_event, _cafe_event()]

    module = _load_voice_omi_helpers(fake_fetch)
    module._pacific_now = lambda: datetime(2026, 5, 7, 15, 0, tzinfo=ZoneInfo("America/Los_Angeles"))

    results = asyncio.run(module._search_canonical_omi_events("uid-1", "what happened this morning", 5, True))

    assert calls == [("uid-1", {"limit": 300, "channels": ["omi"]})]
    assert len(results) == 1
    assert results[0]["title"] == "Cafe Coffee and Waffle Stop"
    assert results[0]["timestamp"] == "2026-05-07 11:56 AM PDT"
    assert results[0]["metadata"]["time_window_applied"] is True


def test_search_canonical_omi_events_returns_empty_when_no_useful_match():
    async def fake_fetch(_uid, **_kwargs):
        return [_cafe_event()]

    module = _load_voice_omi_helpers(fake_fetch)

    results = asyncio.run(module._search_canonical_omi_events("uid-1", "dentist appointment", 5, True))

    assert results == []


def test_voice_query_requires_fanout_for_explicit_omi_and_time_queries():
    async def fake_fetch(_uid, **_kwargs):
        return []

    module = _load_voice_omi_helpers(fake_fetch)

    assert module._voice_query_requires_source_fanout("latest OMI conversation") is True
    assert module._voice_query_requires_source_fanout("what OMI conversations happened this morning") is True
    assert module._voice_query_requires_source_fanout("what did I do today") is True
    assert module._voice_query_requires_source_fanout("where did I put my glasses") is False
