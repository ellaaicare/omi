import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

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
    monkeypatch.delenv("ELLA_RETAINED_OWNER_CHANNEL_RUNTIME_ENABLED", raising=False)
    monkeypatch.setattr(chat, "HERMES_CHAT_SESSION_SCOPE", "canonical")

    assert chat._hermes_chat_session_key("ABC123") == "ella:omi:abc123:canonical"
    assert chat._hermes_chat_session_key("User/123") == "ella:omi:user-123:canonical"
    assert chat._hermes_chat_memory_key("User/123") == "ella:omi:user-123:canonical"


def test_retained_owner_chat_separates_mutable_session_from_canonical_memory(monkeypatch):
    monkeypatch.setenv("ELLA_PLATO_UID", "owner-a")
    monkeypatch.setenv("ELLA_RETAINED_OWNER_CHANNEL_RUNTIME_ENABLED", "true")
    monkeypatch.setattr(chat, "HERMES_CHAT_SESSION_SCOPE", "canonical")

    assert chat._hermes_chat_session_key("owner-a") == "ella:omi:owner-a:canonical:channel:ios-chat"
    assert chat._hermes_chat_memory_key("owner-a") == "ella:omi:owner-a:canonical"
    assert chat._hermes_chat_session_key("owner-b") == "ella:omi:owner-b:canonical"


def test_chat_stream_selects_exact_retained_owner_runtime_before_ordinary_authority(monkeypatch):
    runtime = SimpleNamespace(provider="hermes")
    captured = {"retained": 0, "ordinary": 0, "stream_runtime": None}

    async def retained(uid):
        captured["retained"] += 1
        assert uid == "owner-a"
        return runtime

    async def ordinary(*_args, **_kwargs):
        captured["ordinary"] += 1
        raise AssertionError("ordinary runtime must not be selected")

    def stream(*_args, runtime=None, **_kwargs):
        captured["stream_runtime"] = runtime

        async def response_body():
            yield "done: synthetic\n\n"

        return response_body()

    monkeypatch.setattr(chat, "resolve_retained_owner_channel_runtime", retained)
    monkeypatch.setattr(chat, "resolve_isolated_runtime", ordinary)
    monkeypatch.setattr(chat, "_stream_hermes_chat", stream)
    monkeypatch.setattr(chat, "record_trace", lambda _trace: None)

    response = asyncio.run(
        chat.ella_chat_stream(
            chat.EllaChatRequest(message="content-free test"),
            None,
            "owner-a",
            None,
            "ios",
            None,
            None,
        )
    )

    assert response.media_type == "text/event-stream"
    assert captured == {"retained": 1, "ordinary": 0, "stream_runtime": runtime}


def test_hermes_chat_headers_include_stable_session_key():
    headers = chat._hermes_chat_headers("ella:omi:abc123:ios-chat:daily-20260530", "ella:omi:abc123:canonical")

    assert headers["X-Hermes-Session-Id"] == "ella:omi:abc123:ios-chat:daily-20260530"
    assert headers["X-Hermes-Session-Key"] == "ella:omi:abc123:canonical"
    assert headers["Content-Type"] == "application/json"
