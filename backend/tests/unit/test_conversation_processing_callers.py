import asyncio
import importlib
import importlib.util
import json
import sys
import types
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.websockets import WebSocketState

from models.conversation import Conversation, ConversationStatus, Structured

BACKEND = Path(__file__).resolve().parents[2]


def _noop(*_args, **_kwargs):
    return None


async def _async_noop(*_args, **_kwargs):
    return None


async def _async_call(function, *args, **kwargs):
    return function(*args, **kwargs)


def _stub_module(monkeypatch, name, **attrs):
    module = types.ModuleType(name)
    for attr_name, value in attrs.items():
        setattr(module, attr_name, value)
    monkeypatch.setitem(sys.modules, name, module)
    if "." in name:
        parent_name, child_name = name.rsplit(".", 1)
        parent = sys.modules.get(parent_name)
        if parent is not None:
            monkeypatch.setattr(parent, child_name, module, raising=False)
    return module


def _load_source_module(monkeypatch, name, relative_path):
    spec = importlib.util.spec_from_file_location(name, BACKEND / relative_path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


class _STTService(str, Enum):
    deepgram = "deepgram"
    soniox = "soniox"
    grok = "grok"
    speechmatics = "speechmatics"


class _DummyTask:
    def cancel(self):
        return True


class _FakeWebSocket:
    def __init__(self):
        self.sent_bytes = []
        self.sent_json = []
        self.sent_text = []
        self.closed = []
        self.client_state = WebSocketState.CONNECTED

    async def send_bytes(self, payload):
        self.sent_bytes.append(payload)

    async def send_json(self, payload):
        self.sent_json.append(payload)

    async def send_text(self, payload):
        self.sent_text.append(payload)

    async def close(self, **kwargs):
        self.closed.append(kwargs)
        self.client_state = WebSocketState.DISCONNECTED


def _conversation():
    created_at = datetime(2026, 8, 12, 20, 0, tzinfo=timezone.utc)
    return Conversation(
        id="caller-race-conversation",
        created_at=created_at,
        started_at=created_at,
        finished_at=created_at,
        structured=Structured(title="Caller boundary", overview="Durable result."),
        transcript_segments=[],
        status=ConversationStatus.processing,
        discarded=False,
    )


@pytest.fixture
def caller_modules(monkeypatch):
    for package in (
        "database",
        "utils",
        "utils.conversations",
        "utils.ella",
        "utils.llm",
        "utils.other",
        "utils.stt",
        "ella",
        "ella.routers",
        "ella.services",
    ):
        importlib.import_module(package)

    conversation_data = _conversation().model_dump()
    conversations_db = _stub_module(
        monkeypatch,
        "database.conversations",
        get_conversation=lambda *_args, **_kwargs: dict(conversation_data),
        get_processing_conversations=lambda *_args, **_kwargs: [],
        update_conversation_status=_noop,
        list_capture_persistence_batches=lambda *_args, **_kwargs: [],
        commit_capture_persistence_batch=_noop,
    )
    users_db = _stub_module(
        monkeypatch,
        "database.users",
        db=SimpleNamespace(),
        is_exists_user=lambda *_args, **_kwargs: True,
        get_user_private_cloud_sync_enabled=lambda *_args, **_kwargs: False,
        get_user_transcription_preferences=lambda *_args, **_kwargs: {},
        get_user_conversation_lifecycle_preferences=lambda *_args, **_kwargs: {},
    )
    redis_db = _stub_module(
        monkeypatch,
        "database.redis_db",
        get_cached_user_geolocation=lambda *_args, **_kwargs: None,
        try_acquire_listen_lock=lambda *_args, **_kwargs: True,
    )
    _stub_module(monkeypatch, "database.calendar_meetings")

    app_integrations = _stub_module(
        monkeypatch,
        "utils.app_integrations",
        trigger_external_integrations=lambda *_args, **_kwargs: [],
        trigger_realtime_integrations=_async_noop,
        trigger_realtime_audio_bytes=_async_noop,
    )
    conversation_processor = _stub_module(
        monkeypatch,
        "utils.conversations.process_conversation",
        mark_unexpected_conversation_processing_failed=_noop,
        process_conversation_with_outcome=lambda *_args, **_kwargs: None,
        process_conversation_with_transcript_redelivery=lambda *_args, **_kwargs: None,
        retrieve_in_progress_conversation=lambda *_args, **_kwargs: None,
    )
    _stub_module(monkeypatch, "utils.conversations.location", get_google_maps_location=lambda *_args: None)
    _stub_module(monkeypatch, "utils.apps", is_audio_bytes_app_enabled=lambda *_args: False)
    _stub_module(
        monkeypatch,
        "utils.webhooks",
        send_audio_bytes_developer_webhook=_async_noop,
        realtime_transcript_webhook=_async_noop,
        get_audio_bytes_webhook_seconds=lambda *_args: 0,
    )
    _stub_module(monkeypatch, "utils.other.storage", upload_audio_chunk=_noop)
    _stub_module(monkeypatch, "utils.other.task", safe_create_task=_noop)
    _stub_module(monkeypatch, "utils.speaker_identification", extract_speaker_samples=_noop)

    pusher = _load_source_module(monkeypatch, "_caller_boundary_pusher", "routers/pusher.py")
    monkeypatch.setattr(pusher.asyncio, "to_thread", _async_call)

    _stub_module(monkeypatch, "av", open=_noop, AudioFrame=SimpleNamespace(from_ndarray=_noop))
    _stub_module(monkeypatch, "opuslib", Decoder=object)
    _stub_module(monkeypatch, "lc3", Decoder=object)
    firebase_admin = _stub_module(monkeypatch, "firebase_admin")

    class _InvalidIdTokenError(Exception):
        pass

    firebase_auth = _stub_module(monkeypatch, "firebase_admin.auth", InvalidIdTokenError=_InvalidIdTokenError)
    firebase_admin.auth = firebase_auth
    _stub_module(
        monkeypatch,
        "utils.speaker_assignment",
        process_speaker_assigned_segments=_noop,
        update_speaker_assignment_maps=_noop,
        should_update_speaker_to_person_map=lambda *_args: False,
    )
    _stub_module(monkeypatch, "utils.analytics", record_usage=_noop)
    _stub_module(
        monkeypatch,
        "utils.capture_buffer",
        PusherTranscriptBatch=dict,
        acknowledge_capture_persistence_batch=_noop,
        capture_buffer_contains_conversation=lambda *_args: False,
        deliver_all_pusher_transcript_batches=_async_noop,
        prepare_conversation_bound_capture_batch=_noop,
        queue_pusher_transcript_batch=_noop,
    )
    _stub_module(
        monkeypatch,
        "utils.ella.scanner_keyterms",
        cache_status=lambda *_args: {},
        combine_deepgram_keyterms=lambda vocabulary, _keyterms: vocabulary,
        get_scanner_keyterms=lambda *_args: _async_value([]),
    )
    _stub_module(
        monkeypatch,
        "utils.notifications",
        send_credit_limit_notification=_async_noop,
        send_silent_user_notification=_async_noop,
    )
    _stub_module(monkeypatch, "utils.other.endpoints", get_current_user_uid_from_ws_message=lambda *_args: "uid-1")
    _stub_module(
        monkeypatch,
        "utils.other.storage",
        get_profile_audio_if_exists=lambda *_args: None,
        get_user_has_speech_profile=lambda *_args: False,
        upload_audio_chunk=_noop,
    )
    _stub_module(monkeypatch, "utils.pusher", connect_to_trigger_pusher=_async_noop)
    _stub_module(monkeypatch, "utils.speaker_identification", detect_speaker_from_text=lambda *_args: None)
    _stub_module(
        monkeypatch,
        "utils.stt.streaming",
        SPEECH_PROFILE_FIXED_DURATION=0,
        SPEECH_PROFILE_PADDING_DURATION=0,
        SPEECH_PROFILE_STABILIZE_DELAY=0,
        STTService=_STTService,
        get_stt_service_for_language=lambda *_args, **_kwargs: (_STTService.deepgram, "en", "test-model"),
        process_audio_dg=_async_noop,
        process_audio_grok=_async_noop,
        process_audio_soniox=_async_noop,
        process_audio_speechmatics=_async_noop,
        send_initial_file_path=_async_noop,
    )
    _stub_module(
        monkeypatch,
        "utils.subscription",
        has_transcription_credits=lambda *_args: True,
        get_remaining_transcription_seconds=lambda *_args: None,
    )
    _stub_module(monkeypatch, "utils.translation", TranslationService=object)
    _stub_module(monkeypatch, "utils.translation_cache", TranscriptSegmentLanguageCache=object)
    _stub_module(monkeypatch, "utils.onboarding", OnboardingHandler=object)
    _stub_module(
        monkeypatch,
        "ella.routers.auto_provision",
        auto_provision_user=_async_noop,
        ensure_firestore_user_document=_async_noop,
        get_agent_cluster=_async_noop,
        listen_runtime_gate=_async_noop,
    )
    _stub_module(
        monkeypatch,
        "ella.services.ai_consent",
        assert_current_ai_consent=_noop,
        require_current_ai_consent=lambda: "uid-1",
        resolve_processor=lambda *_args: object(),
    )
    _stub_module(monkeypatch, "utils.aac", AACDecoder=object)
    _stub_module(monkeypatch, "utils.audio", AudioRingBuffer=object)
    _stub_module(
        monkeypatch,
        "utils.stt.speaker_embedding",
        extract_embedding_from_bytes=_async_noop,
        compare_embeddings=lambda *_args: 0,
        SPEAKER_MATCH_THRESHOLD=1,
    )
    _stub_module(monkeypatch, "utils.speaker_sample_migration", maybe_migrate_person_samples=_async_noop)
    _stub_module(monkeypatch, "utils.llm.clients", set_ella_context=_noop)

    transcribe = _load_source_module(monkeypatch, "_caller_boundary_transcribe", "routers/transcribe.py")

    _stub_module(monkeypatch, "routers.conversations", trigger_external_integrations=_noop)
    _stub_module(monkeypatch, "database.apps", get_app_by_id_db=lambda *_args, **_kwargs: SimpleNamespace())
    _stub_module(monkeypatch, "database.memories")
    _stub_module(monkeypatch, "database.notifications")
    _stub_module(monkeypatch, "database.action_items")
    monkeypatch.setattr(redis_db, "get_enabled_apps", lambda *_args, **_kwargs: ["app-1"], raising=False)
    monkeypatch.setattr(redis_db, "r", SimpleNamespace(), raising=False)
    apps_module = sys.modules["utils.apps"]
    monkeypatch.setattr(apps_module, "verify_api_key", lambda *_args, **_kwargs: True, raising=False)
    monkeypatch.setattr(apps_module, "app_can_read_tasks", lambda *_args, **_kwargs: False, raising=False)
    monkeypatch.setattr(apps_module, "app_can_create_conversation", lambda *_args, **_kwargs: True, raising=False)
    monkeypatch.setattr(app_integrations, "send_app_notification", _noop, raising=False)
    _stub_module(monkeypatch, "utils.conversations.memories", process_external_integration_memory=_noop)
    _stub_module(monkeypatch, "utils.conversations.search", search_conversations=lambda *_args, **_kwargs: [])

    workflow = _load_source_module(monkeypatch, "_caller_boundary_workflow", "routers/workflow.py")
    integration = _load_source_module(monkeypatch, "_caller_boundary_integration", "routers/integration.py")
    return SimpleNamespace(
        pusher=pusher,
        transcribe=transcribe,
        workflow=workflow,
        integration=integration,
        conversations_db=conversations_db,
        users_db=users_db,
        redis_db=redis_db,
        app_integrations=app_integrations,
        conversation_processor=conversation_processor,
    )


async def _async_value(value):
    return value


@pytest.mark.parametrize("status", ["already_completed", "stock_summary_cas_lost", "stock_summary_transcript_changed"])
def test_pusher_does_not_dispatch_external_integrations_for_non_dispatch_outcomes(monkeypatch, caller_modules, status):
    calls = []
    conversation = _conversation()
    monkeypatch.setattr(
        caller_modules.pusher,
        "process_conversation_with_transcript_redelivery",
        lambda *_args, **_kwargs: SimpleNamespace(
            conversation=conversation,
            dispatched=False,
            status=status,
        ),
    )
    monkeypatch.setattr(
        caller_modules.pusher,
        "trigger_external_integrations",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    websocket = _FakeWebSocket()

    asyncio.run(
        caller_modules.pusher._process_conversation_task(
            "uid-1",
            conversation.id,
            "en",
            websocket,
        )
    )

    assert calls == []
    assert len(websocket.sent_bytes) == 1
    response = json.loads(websocket.sent_bytes[0][4:].decode())
    if status == "already_completed":
        assert response == {"conversation_id": conversation.id, "success": True}
    else:
        assert response == {"conversation_id": conversation.id, "error": status}


def test_workflow_does_not_dispatch_external_integrations_for_non_dispatch_outcome(monkeypatch, caller_modules):
    calls = []
    conversation = _conversation()
    create_memory = caller_modules.workflow.conversation_models.ExternalIntegrationCreateConversation(
        text="Content-free workflow test.",
        language="en",
    )
    monkeypatch.setenv("WORKFLOW_API_KEY", "workflow-secret")
    monkeypatch.setattr(
        caller_modules.workflow,
        "process_conversation_with_outcome",
        lambda *_args, **_kwargs: SimpleNamespace(
            conversation=conversation,
            dispatched=False,
            status="already_completed",
        ),
    )
    monkeypatch.setattr(
        caller_modules.workflow,
        "trigger_external_integrations",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    result = caller_modules.workflow.create_memory(
        request=None,
        uid="uid-1",
        api_key="workflow-secret",
        create_memory=create_memory,
    )

    assert result == {}
    assert calls == []


def test_integration_does_not_dispatch_external_integrations_for_non_dispatch_outcome(monkeypatch, caller_modules):
    calls = []
    conversation = _conversation()
    create_conversation = caller_modules.integration.conversation_models.ExternalIntegrationCreateConversation(
        text="Content-free integration test.",
        language="en",
    )
    monkeypatch.setattr(caller_modules.integration, "verify_api_key", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        caller_modules.integration.apps_db,
        "get_app_by_id_db",
        lambda *_args, **_kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(caller_modules.integration.redis_db, "get_enabled_apps", lambda _uid: ["app-1"])
    monkeypatch.setattr(caller_modules.integration.apps_utils, "app_can_create_conversation", lambda _app: True)
    monkeypatch.setattr(
        caller_modules.integration,
        "process_conversation_with_outcome",
        lambda *_args, **_kwargs: SimpleNamespace(
            conversation=conversation,
            dispatched=False,
            status="stock_summary_cas_lost",
        ),
    )
    monkeypatch.setattr(
        caller_modules.integration,
        "trigger_external_integrations",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    result = asyncio.run(
        caller_modules.integration.create_conversation_via_integration(
            request=None,
            app_id="app-1",
            create_conversation=create_conversation,
            uid="uid-1",
            authorization="Bearer test-key",
        )
    )

    assert result == {}
    assert calls == []


def test_pusher_dispatches_external_integrations_once_for_winning_outcome(monkeypatch, caller_modules):
    calls = []
    conversation = _conversation()
    monkeypatch.setattr(
        caller_modules.pusher,
        "process_conversation_with_transcript_redelivery",
        lambda *_args, **_kwargs: SimpleNamespace(
            conversation=conversation,
            dispatched=True,
            status="committed",
        ),
    )
    monkeypatch.setattr(
        caller_modules.pusher,
        "trigger_external_integrations",
        lambda *args, **kwargs: calls.append((args, kwargs)) or ["sent"],
    )
    websocket = _FakeWebSocket()

    asyncio.run(
        caller_modules.pusher._process_conversation_task(
            "uid-1",
            conversation.id,
            "en",
            websocket,
        )
    )

    assert len(calls) == 1
    assert calls[0][0] == ("uid-1", conversation)
    assert len(websocket.sent_bytes) == 1


class _FallbackCaptured(Exception):
    pass


def _capture_transcribe_fallback(module, websocket, monkeypatch):
    captured = {}

    def fake_create_task(coroutine):
        coroutine.close()
        return _DummyTask()

    monkeypatch.setattr(module.asyncio, "create_task", fake_create_task)

    target_code = module._stream_handler.__code__

    def trace(frame, event, _arg):
        if event == "line" and frame.f_code is target_code:
            fallback = frame.f_locals.get("_create_conversation_fallback")
            if fallback is not None:
                captured["fallback"] = fallback
                raise _FallbackCaptured
        return trace

    previous_trace = sys.gettrace()
    sys.settrace(trace)
    try:
        with pytest.raises(_FallbackCaptured):
            asyncio.run(
                module._stream_handler(
                    websocket,
                    "uid-1",
                    language="en",
                    custom_stt_mode=module.CustomSttMode.enabled,
                )
            )
    finally:
        sys.settrace(previous_trace)

    return captured["fallback"]


@pytest.mark.parametrize("status", ["already_completed", "stock_summary_cas_lost"])
def test_transcribe_fallback_does_not_dispatch_external_integrations_for_non_dispatch_outcomes(
    monkeypatch, caller_modules, status
):
    calls = []
    conversation = _conversation()
    websocket = _FakeWebSocket()
    monkeypatch.setattr(
        caller_modules.transcribe,
        "process_conversation_with_transcript_redelivery",
        lambda *_args, **_kwargs: SimpleNamespace(
            conversation=conversation,
            dispatched=False,
            status=status,
        ),
    )
    monkeypatch.setattr(
        caller_modules.transcribe,
        "trigger_external_integrations",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    fallback = _capture_transcribe_fallback(caller_modules.transcribe, websocket, monkeypatch)
    asyncio.run(fallback(conversation.model_dump()))

    assert calls == []
