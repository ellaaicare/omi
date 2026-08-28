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
        monkeypatch.setattr(chat, "_produce_hermes_chat_events", producer)
        chat._hermes_chat_turn_tasks.clear()

        stream = chat._stream_hermes_chat("Question", "uid-a", turn_id="turn-a")
        assert await anext(stream) == ": keepalive\n\n"
        await stream.aclose()

        release.set()
        await asyncio.wait_for(completed.wait(), timeout=1)
        await asyncio.sleep(0)

        assert chat._hermes_chat_turn_tasks == {}

    asyncio.run(scenario())


def test_fragmented_hermes_turn_discards_partial_text(monkeypatch):
    async def scenario():
        async def producer(*args, **kwargs):
            yield "data: Partial answer\n\n"
            yield "data: Error: hermes_stream_incomplete\n\n"

        monkeypatch.setattr(chat, "_produce_hermes_chat_events", producer)
        chat._hermes_chat_turn_tasks.clear()

        events = [event async for event in chat._stream_hermes_chat("Question", "uid-a", turn_id="turn-a")]
        await asyncio.sleep(0)

        assert events == ["data: Error: hermes_stream_incomplete\n\n"]
        assert chat._hermes_chat_turn_tasks == {}

    asyncio.run(scenario())
