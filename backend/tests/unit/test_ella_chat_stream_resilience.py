import asyncio

from ella.routers import chat


def test_reconnecting_subscriber_shares_inflight_hermes_turn(monkeypatch):
    async def scenario():
        release = asyncio.Event()
        calls = 0

        async def producer(*args, **kwargs):
            nonlocal calls
            calls += 1
            await release.wait()
            yield "data: Answer\n\n"
            yield "done: terminal\n\n"

        monkeypatch.setattr(chat, "HERMES_CHAT_KEEPALIVE_SECONDS", 0.01)
        monkeypatch.setattr(chat, "HERMES_CHAT_REPLAY_SECONDS", 0.01)
        monkeypatch.setattr(chat, "_produce_hermes_chat_events", producer)
        chat._hermes_chat_turn_tasks.clear()

        first = chat._stream_hermes_chat("Question", "uid-a", turn_id="turn-a")
        assert await anext(first) == ": keepalive\n\n"

        replacement = chat._stream_hermes_chat("Question", "uid-a", turn_id="turn-a")
        assert await anext(replacement) == ": keepalive\n\n"
        await first.aclose()

        release.set()
        events = [event async for event in replacement]
        await asyncio.sleep(0)

        assert calls == 1
        assert events == ["data: Answer\n\n", "done: terminal\n\n"]
        await asyncio.sleep(0.02)
        assert chat._hermes_chat_turn_tasks == {}

    asyncio.run(scenario())


def test_disconnected_subscriber_does_not_cancel_hermes_turn(monkeypatch):
    async def scenario():
        release = asyncio.Event()
        completed = asyncio.Event()

        async def producer(*args, **kwargs):
            await release.wait()
            completed.set()
            yield "done: terminal\n\n"

        monkeypatch.setattr(chat, "HERMES_CHAT_KEEPALIVE_SECONDS", 0.01)
        monkeypatch.setattr(chat, "HERMES_CHAT_REPLAY_SECONDS", 0.01)
        monkeypatch.setattr(chat, "_produce_hermes_chat_events", producer)
        chat._hermes_chat_turn_tasks.clear()

        stream = chat._stream_hermes_chat("Question", "uid-a", turn_id="turn-a")
        assert await anext(stream) == ": keepalive\n\n"
        await stream.aclose()

        release.set()
        await asyncio.wait_for(completed.wait(), timeout=1)
        await asyncio.sleep(0)

        assert chat._hermes_chat_turn_tasks != {}
        await asyncio.sleep(0.02)
        assert chat._hermes_chat_turn_tasks == {}

    asyncio.run(scenario())


def test_fragmented_hermes_turn_discards_partial_text(monkeypatch):
    async def scenario():
        async def producer(*args, **kwargs):
            yield "data: Partial answer\n\n"
            yield "data: Error: hermes_stream_incomplete\n\n"

        monkeypatch.setattr(chat, "_produce_hermes_chat_events", producer)
        monkeypatch.setattr(chat, "HERMES_CHAT_REPLAY_SECONDS", 0.01)
        chat._hermes_chat_turn_tasks.clear()

        events = [event async for event in chat._stream_hermes_chat("Question", "uid-a", turn_id="turn-a")]
        await asyncio.sleep(0)

        assert events == ["data: Error: hermes_stream_incomplete\n\n"]
        await asyncio.sleep(0.02)
        assert chat._hermes_chat_turn_tasks == {}

    asyncio.run(scenario())


def test_completed_turn_is_replayed_without_duplicate_provider_work(monkeypatch):
    async def scenario():
        calls = 0

        async def producer(*args, **kwargs):
            nonlocal calls
            calls += 1
            yield "data: Answer\n\n"
            yield "done: terminal\n\n"

        monkeypatch.setattr(chat, "HERMES_CHAT_REPLAY_SECONDS", 0.01)
        monkeypatch.setattr(chat, "_produce_hermes_chat_events", producer)
        chat._hermes_chat_turn_tasks.clear()

        first = [event async for event in chat._stream_hermes_chat("Question", "uid-a", turn_id="turn-a")]
        await asyncio.sleep(0)
        replacement = [event async for event in chat._stream_hermes_chat("Question", "uid-a", turn_id="turn-a")]

        assert calls == 1
        assert first == replacement == ["data: Answer\n\n", "done: terminal\n\n"]
        await asyncio.sleep(0.02)
        assert chat._hermes_chat_turn_tasks == {}

    asyncio.run(scenario())


def test_same_turn_id_with_different_payload_fails_closed(monkeypatch):
    async def scenario():
        release = asyncio.Event()
        calls = 0

        async def producer(*args, **kwargs):
            nonlocal calls
            calls += 1
            await release.wait()
            yield "done: terminal\n\n"

        monkeypatch.setattr(chat, "HERMES_CHAT_KEEPALIVE_SECONDS", 0.01)
        monkeypatch.setattr(chat, "HERMES_CHAT_REPLAY_SECONDS", 0.01)
        monkeypatch.setattr(chat, "_produce_hermes_chat_events", producer)
        chat._hermes_chat_turn_tasks.clear()

        first = chat._stream_hermes_chat("Question A", "uid-a", turn_id="turn-a")
        assert await anext(first) == ": keepalive\n\n"
        conflict = [event async for event in chat._stream_hermes_chat("Question B", "uid-a", turn_id="turn-a")]

        assert conflict == ["data: Error: hermes_turn_conflict\n\n"]
        assert calls == 1
        await first.aclose()
        release.set()
        await asyncio.sleep(0.02)
        assert chat._hermes_chat_turn_tasks == {}

    asyncio.run(scenario())
