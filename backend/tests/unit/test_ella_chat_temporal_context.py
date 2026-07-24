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


def test_memory_talk_uses_conversation_scoped_hermes_session():
    session_key = chat._hermes_memory_talk_session_key("User/123", "Memory/456")

    assert session_key == "ella:omi:user-123:canonical:memory:memory-456"


def test_hermes_chat_headers_include_stable_session_key():
    headers = chat._hermes_chat_headers("ella:omi:abc123:ios-chat:daily-20260530", "ella:omi:abc123:canonical")

    assert headers["X-Hermes-Session-Id"] == "ella:omi:abc123:ios-chat:daily-20260530"
    assert headers["X-Hermes-Session-Key"] == "ella:omi:abc123:canonical"
    assert headers["Content-Type"] == "application/json"


def test_memory_talk_context_contains_only_the_selected_memory_and_linked_people(monkeypatch):
    monkeypatch.setattr(
        chat,
        "_get_memory_conversation",
        lambda uid, conversation_id: {
            "created_at": "2026-07-23T09:40:00Z",
            "structured": {
                "title": "Coffee in the garden with Margaret",
                "overview": "The tomatoes are coming in.",
            },
            "transcript_segments": [
                {"text": "Dinner on Tuesday?", "person_id": "person-1"},
                {"text": "Yes, at six.", "person_id": "person-1"},
            ],
        },
    )
    monkeypatch.setattr(
        chat,
        "_get_linked_people",
        lambda uid, person_ids: [{"id": "person-1", "name": "Margaret"}],
    )

    context = chat._memory_talk_context("uid-1", "memory-1")

    assert "Coffee in the garden with Margaret" in context
    assert "The tomatoes are coming in." in context
    assert "Dinner on Tuesday?" in context
    assert "Margaret" in context


def test_memory_talk_turn_is_not_written_to_main_chat_or_created_as_a_memory(monkeypatch):
    scoped = []
    canonical = []

    async def fake_to_thread(function, **kwargs):
        scoped.append((function, kwargs))

    async def fake_canonical(event):
        canonical.append(event)

    monkeypatch.setattr(chat.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(chat, "_write_ios_chat_canonical_event", fake_canonical)

    asyncio.run(
        chat._persist_chat_turn(
            uid="uid-1",
            conversation_id="memory-1",
            turn_id="turn-1",
            role="user",
            text="I remember the tomatoes.",
            session_key="ella:omi:uid-1:canonical",
            created_at=datetime(2026, 7, 23, 17, 0, tzinfo=timezone.utc),
        )
    )

    assert len(scoped) == 1
    assert scoped[0][0] is chat._persist_memory_talk_turn
    assert scoped[0][1]["conversation_id"] == "memory-1"
    assert canonical == []


def test_memory_talk_route_sends_only_selected_memory_and_scoped_turns_to_hermes(monkeypatch):
    captured = {}
    persisted = []

    async def fail_global_context(*args, **kwargs):
        raise AssertionError("Memory talk must not fetch user-wide context")

    async def fake_persist(**kwargs):
        persisted.append(kwargs)

    class FakeResponse:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def aiter_lines(self):
            yield (
                'data: {"choices":[{"delta":{"content":'
                '"That sounds painful. I can stay with what this memory says without judging anyone."}}]}'
            )
            yield "data: [DONE]"

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        def stream(self, method, url, *, headers, json):
            captured.update(
                {
                    "method": method,
                    "url": url,
                    "headers": headers,
                    "json": json,
                }
            )
            return FakeResponse()

    monkeypatch.setattr(chat, "HERMES_GATEWAY_TOKEN", "test-token")
    monkeypatch.setattr(chat, "_fetch_chat_canonical_events", fail_global_context)
    monkeypatch.setattr(chat, "_fetch_temporal_chat_context", fail_global_context)
    monkeypatch.setattr(
        chat,
        "_load_memory_talk_turns",
        lambda uid, conversation_id: [
            {
                "role": "user",
                "content": "I felt abandoned when my daughter did not call.",
            },
            {
                "role": "assistant",
                "content": "That sounds lonely. I will stay with this memory.",
            },
        ],
    )
    monkeypatch.setattr(chat, "_persist_chat_turn", fake_persist)
    monkeypatch.setattr(chat.httpx, "AsyncClient", FakeClient)

    async def collect():
        return [
            chunk
            async for chunk in chat._stream_hermes_chat(
                "Was she selfish?",
                "uid-1",
                turn_id="turn-1",
                conversation_id="memory-1",
                memory_context=(
                    "Title: A missed phone call\n"
                    "Summary: Margaret expected a call from Rose.\n"
                    "Source conversation:\nRose did not call."
                ),
            )
        ]

    chunks = asyncio.run(collect())

    assert captured["headers"]["X-Hermes-Session-Id"] == "ella:omi:uid-1:canonical:memory:memory-1"
    assert captured["headers"]["X-Hermes-Session-Key"] == "ella:omi:uid-1:canonical:memory:memory-1"
    assert captured["json"]["messages"] == [
        {
            "role": "system",
            "content": (
                f"{chat.MEMORY_TALK_PERSONA_PROMPT}\n\n"
                "Memory context:\n"
                "Title: A missed phone call\n"
                "Summary: Margaret expected a call from Rose.\n"
                "Source conversation:\nRose did not call."
            ),
        },
        {
            "role": "user",
            "content": "I felt abandoned when my daughter did not call.",
        },
        {
            "role": "assistant",
            "content": "That sounds lonely. I will stay with this memory.",
        },
        {"role": "user", "content": "Was she selfish?"},
    ]
    serialized_messages = str(captured["json"]["messages"])
    assert "canonical timeline" not in serialized_messages
    assert "unrelated memory" not in serialized_messages
    assert "without judging" in serialized_messages
    assert any("That sounds painful" in chunk for chunk in chunks)
    assert len(persisted) == 2
    assert all(call["conversation_id"] == "memory-1" for call in persisted)
    assert all(call["session_key"] == "ella:omi:uid-1:canonical:memory:memory-1" for call in persisted)


def test_memory_talk_correction_exchange_is_persisted_under_the_selected_memory(monkeypatch):
    persisted = []
    monkeypatch.setattr(chat, "_get_memory_conversation", lambda uid, conversation_id: {"id": conversation_id})
    monkeypatch.setattr(chat, "_persist_memory_talk_turn", lambda **kwargs: persisted.append(kwargs))

    result = chat.append_memory_talk_turns(
        "memory-1",
        chat.MemoryTalkTurnsAppendRequest(
            turns=[
                chat.MemoryTalkTurnInput(role="user", text="It was Rose, not Margaret."),
                chat.MemoryTalkTurnInput(role="assistant", text="So it was Rose — did I get that right?"),
            ]
        ),
        uid="uid-1",
    )

    assert result["persisted"] == 2
    assert [turn["role"] for turn in persisted] == ["user", "assistant"]
    assert {turn["conversation_id"] for turn in persisted} == {"memory-1"}


def test_memory_talk_history_returns_latest_bounded_window_in_chronological_order(monkeypatch):
    persisted = [
        {
            "id": f"turn-{index}",
            "role": "user",
            "text": f"Turn {index}",
            "created_at": datetime(2026, 7, 23, 17, index, tzinfo=timezone.utc),
        }
        for index in range(5)
    ]

    monkeypatch.setattr(chat, "_get_memory_conversation", lambda uid, conversation_id: {"id": conversation_id})

    def fake_list_turns(uid, conversation_id, limit, *, newest_first=False):
        assert uid == "uid-1"
        assert conversation_id == "memory-1"
        assert limit == 3
        assert newest_first is True
        return list(reversed(persisted))[:limit]

    monkeypatch.setattr(chat.memory_talk_db, "list_turns", fake_list_turns)

    result = chat.memory_talk_history("memory-1", limit=3, uid="uid-1")

    assert [turn["id"] for turn in result["turns"]] == ["turn-2", "turn-3", "turn-4"]
    assert [turn["created_at"] for turn in result["turns"]] == [
        "2026-07-23T17:02:00+00:00",
        "2026-07-23T17:03:00+00:00",
        "2026-07-23T17:04:00+00:00",
    ]
