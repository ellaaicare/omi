import asyncio
from datetime import datetime, timezone

from ella.routers import chat


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
