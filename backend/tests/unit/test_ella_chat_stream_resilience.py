import asyncio
from types import SimpleNamespace

import pytest
from fastapi import Request

from ella.routers import chat
from ella.services.ai_consent import AiConsentHTTPException


@pytest.fixture(autouse=True)
def _current_ai_consent(monkeypatch):
    monkeypatch.setattr(chat, "assert_current_ai_consent", lambda uid: uid)


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


@pytest.mark.parametrize(
    ("status_code", "detail"),
    [
        (403, {"code": "ai_consent_required"}),
        (503, {"code": "ai_consent_authority_unavailable", "retryable": True}),
    ],
)
def test_chat_rechecks_consent_before_stream_headers(monkeypatch, status_code, detail):
    effects = []

    async def isolated_runtime(*_args, **_kwargs):
        effects.append("runtime")
        return SimpleNamespace(provider="hermes", agent_id="agent-a")

    async def forbidden_stream(*_args, **_kwargs):
        effects.append("provider")
        yield "done: forbidden\n\n"

    def reject_after_admission(_uid):
        effects.append("consent-recheck")
        raise AiConsentHTTPException(status_code=status_code, detail=detail)

    monkeypatch.setattr(chat, "resolve_isolated_runtime", isolated_runtime)
    monkeypatch.setattr(chat, "_stream_hermes_chat", forbidden_stream)
    monkeypatch.setattr(chat, "assert_current_ai_consent", reject_after_admission)
    monkeypatch.setattr(chat, "record_trace", lambda *_args, **_kwargs: effects.append("trace"))
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/ella/chat/stream",
            "headers": [],
        }
    )

    with pytest.raises(AiConsentHTTPException) as error:
        asyncio.run(
            chat.ella_chat_stream(
                chat.EllaChatRequest(uid="uid-a", message="content-free"),
                request,
                authenticated_uid="uid-a",
            )
        )

    assert error.value.status_code == status_code
    assert error.value.detail == detail
    assert effects == ["runtime", "consent-recheck"]


@pytest.mark.parametrize(
    ("status_code", "detail", "expected_code"),
    [
        (403, {"code": "ai_consent_required"}, "ai_consent_required"),
        (
            503,
            {"code": "ai_consent_authority_unavailable", "retryable": True},
            "ai_consent_authority_unavailable",
        ),
    ],
)
def test_empty_hermes_stream_rechecks_consent_before_nonstream_fallback(
    monkeypatch,
    status_code,
    detail,
    expected_code,
):
    effects = []
    consent_checks = 0

    def consent(_uid):
        nonlocal consent_checks
        consent_checks += 1
        effects.append(f"consent-{consent_checks}")
        if consent_checks == 2:
            raise AiConsentHTTPException(status_code=status_code, detail=detail)
        return "uid-a"

    class EmptyResponse:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def aiter_lines(self):
            if False:
                yield ""

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def stream(self, *args, **kwargs):
            effects.append("initial-stream")
            return EmptyResponse()

    async def no_events(*args, **kwargs):
        return []

    async def no_temporal(*args, **kwargs):
        return ("", [])

    async def no_write(*args, **kwargs):
        return None

    async def revalidate(_identity):
        return runtime

    async def forbidden_fallback(*args, **kwargs):
        effects.append("fallback-provider")
        return "forbidden"

    runtime = SimpleNamespace(
        profile_name="profile-a",
        provider="hermes",
        gateway_url="http://hermes.test",
        gateway_token="synthetic-token",
        agent_id="agent-a",
    )
    monkeypatch.setattr(chat, "assert_current_ai_consent", consent)
    monkeypatch.setattr(chat.httpx, "AsyncClient", Client)
    monkeypatch.setattr(chat, "_fetch_chat_canonical_events", no_events)
    monkeypatch.setattr(chat, "_fetch_temporal_chat_context", no_temporal)
    monkeypatch.setattr(chat, "_write_ios_chat_canonical_event", no_write)
    monkeypatch.setattr(chat, "runtime_authority_identity", lambda _runtime: object())
    monkeypatch.setattr(chat, "revalidate_runtime_authority", revalidate)
    monkeypatch.setattr(chat, "_hermes_nonstream_completion", forbidden_fallback)

    async def collect():
        return [
            item
            async for item in chat._produce_hermes_chat_events(
                "Synthetic question",
                "uid-a",
                turn_id="turn-a",
                runtime=runtime,
            )
        ]

    output = asyncio.run(collect())

    assert output == [f"data: Error: {expected_code}\n\n"]
    assert effects == ["consent-1", "initial-stream", "consent-2"]


@pytest.mark.parametrize(
    ("status_code", "detail", "expected_code"),
    [
        (403, {"code": "ai_consent_required"}, "ai_consent_required"),
        (
            503,
            {"code": "ai_consent_authority_unavailable", "retryable": True},
            "ai_consent_authority_unavailable",
        ),
    ],
)
def test_cloud_hermes_post_header_consent_race_preserves_exact_sse_code(
    monkeypatch,
    status_code,
    detail,
    expected_code,
):
    effects = []

    class Repository:
        @classmethod
        async def create(cls):
            return object()

    class Service:
        def __init__(self, **kwargs):
            pass

        async def run_turn(self, runtime, request, *, before_provider_call=None):
            effects.append("provider-boundary")
            await before_provider_call()
            effects.append("provider")
            raise AssertionError("provider must remain fenced")

    def reject(_uid):
        effects.append("consent")
        raise AiConsentHTTPException(status_code=status_code, detail=detail)

    async def no_events(*args, **kwargs):
        return []

    async def no_temporal(*args, **kwargs):
        return ("", [])

    runtime = SimpleNamespace(provider="hermes_cloud", binding_id="binding-a")
    monkeypatch.setattr(chat, "assert_current_ai_consent", reject)
    monkeypatch.setattr(chat, "EllaProvisioningRepository", Repository)
    monkeypatch.setattr(chat, "HermesCloudRuntimeService", Service)
    monkeypatch.setattr(chat, "_fetch_chat_canonical_events", no_events)
    monkeypatch.setattr(chat, "_fetch_temporal_chat_context", no_temporal)

    async def collect():
        return [
            item
            async for item in chat._stream_hermes_cloud_chat(
                "Synthetic hello",
                "uid-a",
                {"synthetic": True},
                turn_id="turn-a",
                client_sent_at=chat.datetime.now(chat.timezone.utc),
                runtime=runtime,
            )
        ]

    output = asyncio.run(collect())

    assert output[0] == f"data: Error: {expected_code}\n\n"
    assert len([item for item in output if item.startswith("done: ")]) == 1
    assert effects == ["provider-boundary", "consent"]
