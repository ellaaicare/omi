import ast
import asyncio
import importlib.util
import json
import struct
import sys
import time
import types
from datetime import datetime, timedelta, timezone
from functools import cache
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import List
from unittest.mock import MagicMock, patch

from starlette.websockets import WebSocketDisconnect, WebSocketState

BACKEND = Path(__file__).resolve().parents[2]


class CaptureReconnectAuthorityBusy(RuntimeError):
    pass


@cache
def _nested_code(relative_path: str, name: str) -> types.CodeType:
    root = compile((BACKEND / relative_path).read_text(), relative_path, "exec")
    pending = [root]
    while pending:
        code = pending.pop()
        for constant in code.co_consts:
            if not isinstance(constant, types.CodeType):
                continue
            if constant.co_name == name:
                return constant
            pending.append(constant)
    raise AssertionError(f"nested production function not found: {name}")


def _cell(value):
    return (lambda: value).__closure__[0]


def _nested_function(
    relative_path: str, name: str, globals_: dict, closure_values: dict, *, argdefs=None, shared_cells=None
):
    code = _nested_code(relative_path, name)
    missing = set(code.co_freevars) - set(closure_values)
    assert not missing, f"missing closure values for {name}: {sorted(missing)}"
    shared_cells = shared_cells or {}
    closure = tuple(shared_cells.get(freevar, _cell(closure_values[freevar])) for freevar in code.co_freevars)
    return types.FunctionType(code, {"__builtins__": __builtins__, **globals_}, name, argdefs, closure)


@cache
def _load_conversations_module():
    spec = importlib.util.spec_from_file_location(
        "database.capture_incident_1210_conversations",
        BACKEND / "database" / "conversations.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    with patch.dict(
        sys.modules,
        {
            "database._client": MagicMock(db=MagicMock()),
            "database.users": MagicMock(),
            "database.redis_db": MagicMock(),
            "utils.encryption": MagicMock(),
            "utils.other.storage": MagicMock(),
        },
    ):
        spec.loader.exec_module(module)
    return module


@cache
def _load_capture_protocol_module():
    spec = importlib.util.spec_from_file_location(
        "utils.conversations.capture_incident_1210_protocol",
        BACKEND / "utils" / "conversations" / "capture_protocol.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"database._client": MagicMock(db=MagicMock())}):
        spec.loader.exec_module(module)
    return module


class _InMemoryOwnershipRedis:
    def __init__(self):
        self.values = {}

    def eval(self, script, key_count, *args):
        active_key, owner_key = args[:key_count]
        argv = args[key_count:]
        if "local active_id = redis.call('GET', KEYS[1])" in script:
            conversation_id, owner_id, _ttl = argv
            active_id = self.values.get(active_key)
            if active_id is not None and active_id != conversation_id:
                return 0
            self.values[active_key] = conversation_id
            self.values[owner_key] = owner_id
            return 1
        if "redis.call('SET', KEYS[1], ARGV[1]" in script:
            conversation_id, owner_id, _ttl = argv
            self.values[active_key] = conversation_id
            if owner_id:
                self.values[owner_key] = owner_id
            else:
                self.values.pop(owner_key, None)
            return 1
        if "redis.call('EXPIRE', KEYS[1], ARGV[3])" in script:
            conversation_id, owner_id, _ttl = argv
            return int(self.values.get(active_key) == conversation_id and self.values.get(owner_key) == owner_id)
        raise AssertionError("unexpected Redis script")

    def get(self, key):
        value = self.values.get(key)
        return value.encode() if value is not None else None

    def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)


@cache
def _load_ownership_redis_module():
    redis_module = ModuleType("redis")
    redis_module.Redis = lambda **_kwargs: _InMemoryOwnershipRedis()
    attestation_module = ModuleType("database.honcho_attestation")
    attestation_module.authority_credential = lambda *_args, **_kwargs: ""
    spec = importlib.util.spec_from_file_location(
        "database.capture_incident_1210_redis",
        BACKEND / "database" / "redis_db.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    with patch.dict(
        sys.modules,
        {
            "redis": redis_module,
            "database.honcho_attestation": attestation_module,
        },
    ):
        spec.loader.exec_module(module)
    return module


def test_pusher_send_then_disconnect_without_ack_falls_back_to_local_processing():
    async def send_with_consent(consent_guard, provider_send, payload):
        await consent_guard()
        await provider_send(payload)

    class PusherSocket:
        def __init__(self):
            self.sent = []
            self.closed = False
            self.responses = asyncio.Queue()

        async def send(self, payload):
            self.sent.append(bytes(payload))

        async def close(self, *_args):
            self.closed = True

        async def recv(self):
            return await self.responses.get()

    socket = PusherSocket()

    async def connect_to_pusher(*_args, **_kwargs):
        return socket

    async def deliver_all(_queue, _sender):
        return 0

    handler = _nested_function(
        "routers/transcribe.py",
        "create_pusher_task_handler",
        {
            "asyncio": asyncio,
            "struct": struct,
            "json": json,
            "time": time,
            "List": List,
            "ConnectionClosed": ConnectionError,
            "PusherTranscriptBatch": object,
            "AiConsentWebSocketRejected": RuntimeError,
            "_send_pusher_payload_with_current_consent": send_with_consent,
            "connect_to_trigger_pusher": connect_to_pusher,
            "deliver_all_pusher_transcript_batches": deliver_all,
            "get_audio_bytes_webhook_seconds": lambda _uid: 0,
            "is_audio_bytes_app_enabled": lambda _uid: False,
            "queue_pusher_transcript_batch": lambda *_args: None,
            "PUSHER_PROCESSING_RESPONSE_TIMEOUT_SECONDS": 1.0,
        },
        {
            "current_conversation_id": "conversation-a",
            "language": "en",
            "on_conversation_processed": lambda _conversation_id: None,
            "private_cloud_sync_enabled": False,
            "sample_rate": 8_000,
            "session_id": "socket-a",
            "uid": "uid-a",
            "websocket_active": True,
        },
        argdefs=(None,),
    )
    consent_guard = lambda **_kwargs: asyncio.sleep(0)
    connect, close, *_unused, request_processing, _receive, _connected, _speaker = handler(consent_guard)
    fallback_calls = []

    async def fallback(conversation):
        fallback_calls.append(conversation["id"])

    process = _nested_function(
        "routers/transcribe.py",
        "_process_conversation",
        {
            "complete_rotated_capture": lambda *_args: True,
            "drain_capture_persistence_batches": lambda *_args: None,
            "conversations_db": SimpleNamespace(
                get_conversation=lambda *_args: {
                    "id": "conversation-a",
                    "transcript_segments": [{"id": "segment-a", "text": "captured"}],
                    "photos": [],
                },
                delete_conversation=lambda *_args: None,
            ),
            "PUSHER_ENABLED": True,
        },
        {
            "_create_conversation_fallback": fallback,
            "_latency_log": lambda *_args, **_kwargs: None,
            "_wait_for_capture_buffers_to_drain": lambda _conversation_id: asyncio.sleep(0, result=True),
            "generation_id": "generation-a",
            "on_conversation_processing_started": lambda _conversation_id: None,
            "owner_token": "socket-a",
            "request_conversation_processing": request_processing,
            "session_id": "socket-a",
            "uid": "uid-a",
        },
    )

    async def scenario():
        await connect()
        process_task = asyncio.create_task(process("conversation-a", wait_for_buffers=True))
        while not socket.sent:
            await asyncio.sleep(0)
        assert len(socket.sent) == 1
        assert struct.unpack("I", socket.sent[0][:4])[0] == 104
        await close()
        assert await process_task is True

    asyncio.run(scenario())

    assert socket.closed is True
    assert fallback_calls == ["conversation-a"]

    async def terminal_request(_conversation_id):
        return "terminal_error"

    terminal_process = _nested_function(
        "routers/transcribe.py",
        "_process_conversation",
        {
            "complete_rotated_capture": lambda *_args: True,
            "drain_capture_persistence_batches": lambda *_args: None,
            "conversations_db": SimpleNamespace(
                get_conversation=lambda *_args: {
                    "id": "conversation-a",
                    "transcript_segments": [{"id": "segment-a", "text": "captured"}],
                    "photos": [],
                },
                delete_conversation=lambda *_args: None,
            ),
            "PUSHER_ENABLED": True,
        },
        {
            "_create_conversation_fallback": fallback,
            "_latency_log": lambda *_args, **_kwargs: None,
            "_wait_for_capture_buffers_to_drain": lambda _conversation_id: asyncio.sleep(0, result=True),
            "generation_id": "generation-a",
            "on_conversation_processing_started": lambda _conversation_id: None,
            "owner_token": "socket-a",
            "request_conversation_processing": terminal_request,
            "session_id": "socket-a",
            "uid": "uid-a",
        },
    )
    assert asyncio.run(terminal_process("conversation-a", wait_for_buffers=True)) is True
    assert fallback_calls == ["conversation-a"]


def test_consent_rejection_defers_both_conversation_processing_callers_without_fallback():
    class ConsentRejected(RuntimeError):
        def __init__(self, *, retryable: bool):
            self.retryable = retryable

    class PusherSocket:
        def __init__(self):
            self.sent = []

        async def send(self, payload):
            self.sent.append(bytes(payload))

    socket = PusherSocket()
    rejection = {"retryable": True}

    async def consent_rejected_send(_payload):
        raise ConsentRejected(retryable=rejection["retryable"])

    request_processing = _nested_function(
        "routers/transcribe.py",
        "request_conversation_processing",
        {
            "AiConsentWebSocketRejected": ConsentRejected,
            "asyncio": asyncio,
            "bytes": bytes,
            "json": json,
            "PUSHER_PROCESSING_RESPONSE_TIMEOUT_SECONDS": 1.0,
            "struct": struct,
        },
        {
            "language": "en",
            "pending_conversation_requests": {},
            "pending_request_event": asyncio.Event(),
            "pusher_connected": True,
            "pusher_ws": socket,
            "send_pusher_payload": consent_rejected_send,
            "session_id": "socket-a",
            "uid": "uid-a",
            "websocket_active": True,
        },
    )
    conversation = {
        "id": "conversation-a",
        "status": "processing",
        "transcript_segments": [{"id": "segment-a", "text": "captured"}],
        "photos": [],
    }
    fallback_calls = []

    async def fallback(value):
        fallback_calls.append(value["id"])

    cleanup = _nested_function(
        "routers/transcribe.py",
        "cleanup_processing_conversations",
        {
            "conversations_db": SimpleNamespace(get_processing_conversations=lambda _uid: [conversation]),
            "PUSHER_ENABLED": True,
        },
        {
            "_create_conversation_fallback": fallback,
            "request_conversation_processing": request_processing,
            "session_id": "socket-a",
            "uid": "uid-a",
        },
    )
    process = _nested_function(
        "routers/transcribe.py",
        "_process_conversation",
        {
            "complete_rotated_capture": lambda *_args: True,
            "conversations_db": SimpleNamespace(
                get_conversation=lambda *_args: conversation,
                delete_conversation=lambda *_args, **_kwargs: None,
            ),
            "PUSHER_ENABLED": True,
        },
        {
            "_create_conversation_fallback": fallback,
            "_latency_log": lambda *_args, **_kwargs: None,
            "_wait_for_capture_buffers_to_drain": lambda _conversation_id: asyncio.sleep(0, result=True),
            "generation_id": "generation-a",
            "on_conversation_processing_started": lambda _conversation_id: None,
            "owner_token": "socket-a",
            "request_conversation_processing": request_processing,
            "session_id": "socket-a",
            "uid": "uid-a",
        },
    )

    async def scenario():
        assert await request_processing("conversation-a") == "consent_deferred"
        await cleanup()
        assert await process("conversation-a", wait_for_buffers=True) is True
        rejection["retryable"] = False
        assert await request_processing("conversation-a") == "consent_required"
        await cleanup()
        assert await process("conversation-a", wait_for_buffers=True) is True

    asyncio.run(scenario())

    assert socket.sent == []
    assert fallback_calls == []
    assert conversation["status"] == "processing"


def _rotated_capture_consent_harness():
    conversations = _load_conversations_module()
    predecessor = {
        "id": "conversation-a",
        "status": "in_progress",
        "capture_owner_id": "socket-a",
        "transcript_segments": [{"id": "segment-a", "text": "captured"}],
        "photos": [],
    }
    successor = {
        "id": "conversation-b",
        "status": "in_progress",
        "capture_owner_id": None,
    }

    class Ref:
        def __init__(self, ref_id, data):
            self.id = ref_id
            self.data = data

        def get(self, transaction=None):
            return SimpleNamespace(exists=True, to_dict=lambda: dict(self.data))

    class Transaction:
        def __init__(self):
            self.updates = []

        def update(self, ref, payload):
            self.updates.append((ref, payload))

        def apply(self):
            for ref, payload in self.updates:
                ref.data.update(payload)

    predecessor_ref = Ref("conversation-a", predecessor)
    successor_ref = Ref("conversation-b", successor)

    def rotate(next_owner_id):
        successor["capture_owner_id"] = next_owner_id
        transaction = Transaction()
        transferred = conversations._transfer_capture_conversation_owner_transaction(
            transaction,
            predecessor_ref,
            successor_ref,
            "socket-a",
            next_owner_id,
        )
        assert transferred is True
        transaction.apply()
        return True

    def activate():
        transaction = Transaction()
        activated = conversations._activate_capture_conversation_processing_transaction(
            transaction,
            predecessor_ref,
            successor_ref.id,
        )
        if activated:
            transaction.apply()
        return activated

    fallback_calls = []
    provider_dispatches = []
    processing_started = []
    completion_attempts = []
    state = {"authority_available": False}

    class Repository:
        @staticmethod
        def upsert_conversation(_uid, conversation_data):
            assert conversation_data["id"] == successor["id"]
            successor.update(conversation_data)

        @staticmethod
        def transfer_capture_conversation_owner(
            _uid,
            previous_conversation_id,
            expected_previous_owner_id,
            next_conversation_id,
            next_owner_id,
        ):
            assert previous_conversation_id == predecessor["id"]
            assert expected_previous_owner_id == "socket-a"
            assert next_conversation_id == successor["id"]
            return rotate(next_owner_id)

        @staticmethod
        def activate_capture_conversation_processing(_uid, conversation_id, expected_successor_id):
            assert conversation_id == predecessor["id"]
            assert expected_successor_id == successor["id"]
            return activate()

        @staticmethod
        def get_conversation(_uid, conversation_id):
            assert conversation_id == predecessor["id"]
            return predecessor

        @staticmethod
        def get_processing_conversations(_uid):
            return [predecessor] if predecessor["status"] == "processing" else []

        @staticmethod
        def delete_conversation(*_args, **_kwargs):
            raise AssertionError("captured content must not be deleted")

        @staticmethod
        def rollback_capture_conversation_owner_transfer(*_args):
            raise AssertionError("successful publication must not roll back")

        @staticmethod
        def abandon_capture_conversation_if_owned(*_args):
            raise AssertionError("successful publication must not abandon the successor")

    async def fallback(conversation):
        fallback_calls.append(conversation["id"])

    async def request_processing(conversation_id):
        if not state["authority_available"]:
            return "consent_deferred"
        provider_dispatches.append(conversation_id)
        predecessor["status"] = "completed"
        return "processed"

    async def buffers_drained(_conversation_id, **_kwargs):
        return True

    def complete_rotated_capture(*_args):
        completion_attempts.append(predecessor["id"])
        return False

    repository = Repository()
    process = _nested_function(
        "routers/transcribe.py",
        "_process_conversation",
        {
            "complete_rotated_capture": complete_rotated_capture,
            "conversations_db": repository,
            "PUSHER_ENABLED": True,
        },
        {
            "_create_conversation_fallback": fallback,
            "_latency_log": lambda *_args, **_kwargs: None,
            "_wait_for_capture_buffers_to_drain": buffers_drained,
            "generation_id": "generation-a",
            "on_conversation_processing_started": processing_started.append,
            "owner_token": "socket-a",
            "request_conversation_processing": request_processing,
            "session_id": "socket-a",
            "uid": "uid-a",
        },
    )
    cleanup = _nested_function(
        "routers/transcribe.py",
        "cleanup_processing_conversations",
        {
            "conversations_db": repository,
            "PUSHER_ENABLED": True,
        },
        {
            "_create_conversation_fallback": fallback,
            "request_conversation_processing": request_processing,
            "session_id": "socket-a",
            "uid": "uid-a",
        },
    )
    return SimpleNamespace(
        cleanup=cleanup,
        completion_attempts=completion_attempts,
        fallback_calls=fallback_calls,
        predecessor=predecessor,
        process=process,
        processing_started=processing_started,
        provider_dispatches=provider_dispatches,
        repository=repository,
        activate=activate,
        rotate=rotate,
        state=state,
    )


def test_silence_rotation_keeps_deferred_consent_predecessor_retryable_until_recovery():
    harness = _rotated_capture_consent_harness()
    harness.rotate("socket-b")
    assert harness.activate() is True

    async def process_with_default(conversation_id):
        return await harness.process(conversation_id, wait_for_buffers=True)

    process_after_rotation = _nested_function(
        "routers/transcribe.py",
        "_process_conversation_after_rotation",
        {"asyncio": asyncio},
        {"_process_conversation": process_with_default},
    )

    asyncio.run(process_after_rotation("conversation-a"))

    assert harness.predecessor["status"] == "processing"
    assert harness.predecessor["capture_owner_id"] is None
    assert harness.processing_started == ["conversation-a"]
    assert harness.completion_attempts == ["conversation-a"]
    assert harness.fallback_calls == []
    assert harness.provider_dispatches == []

    harness.state["authority_available"] = True
    asyncio.run(harness.cleanup())
    assert harness.provider_dispatches == ["conversation-a"]
    assert harness.predecessor["status"] == "completed"


def test_disconnect_rotation_keeps_deferred_consent_predecessor_retryable_until_recovery():
    harness = _rotated_capture_consent_harness()

    class Conversation:
        def __init__(self, **values):
            self.values = values

        def dict(self):
            return dict(self.values)

    class Redis:
        @staticmethod
        def rotate_in_progress_conversation_id(
            _uid,
            expected_conversation_id,
            expected_owner_id,
            new_conversation_id,
            new_owner_id,
        ):
            assert expected_conversation_id == "conversation-a"
            assert expected_owner_id == "socket-a"
            assert new_conversation_id == "conversation-b"
            assert new_owner_id is None
            return True

    async def publish_ready(*_args, **_kwargs):
        raise AssertionError("disconnect rotation must not publish active socket authority")

    create_stub = _nested_function(
        "routers/transcribe.py",
        "_create_new_in_progress_conversation",
        {
            "Conversation": Conversation,
            "ConversationSource": SimpleNamespace(omi="omi", desktop="desktop"),
            "ConversationStatus": SimpleNamespace(in_progress="in_progress"),
            "Structured": dict,
            "calendar_db": SimpleNamespace(get_meetings_in_time_range=lambda *_args: []),
            "conversations_db": harness.repository,
            "datetime": datetime,
            "redis_db": Redis(),
            "timedelta": timedelta,
            "timezone": timezone,
            "uuid": SimpleNamespace(uuid4=lambda: "conversation-b"),
        },
        {
            "_latency_log": lambda *_args, **_kwargs: None,
            "_publish_capture_protocol_ready": publish_ready,
            "current_conversation_id": "conversation-a",
            "language": "en",
            "private_cloud_sync_enabled": False,
            "session_id": "socket-a",
            "source": None,
            "uid": "uid-a",
            "websocket_active": True,
        },
    )

    async def create_successor(**kwargs):
        options = {
            "expected_conversation_id": None,
            "expected_owner_id": None,
            "replace_stale_conversation_id": None,
            "new_owner_id": "socket-a",
            "adopt": True,
        }
        options.update(kwargs)
        return await create_stub(**options)

    async def buffers_drained(_conversation_id, **_kwargs):
        return True

    finalize_disconnect = _nested_function(
        "routers/transcribe.py",
        "_finalize_current_conversation_on_disconnect",
        {
            "ConversationStatus": SimpleNamespace(in_progress="in_progress"),
            "conversations_db": SimpleNamespace(
                get_conversation=lambda _uid, _conversation_id: harness.predecessor,
            ),
            "drain_capture_persistence_batches": lambda *_args: None,
        },
        {
            "_create_new_in_progress_conversation": create_successor,
            "_latency_log": lambda *_args, **_kwargs: None,
            "_process_conversation": harness.process,
            "_wait_for_capture_buffers_to_drain": buffers_drained,
            "current_conversation_id": "conversation-a",
            "generation_id": "generation-a",
            "session_id": "socket-a",
            "uid": "uid-a",
        },
    )

    asyncio.run(finalize_disconnect())

    assert harness.predecessor["status"] == "processing"
    assert harness.predecessor["capture_owner_id"] is None
    assert harness.processing_started == ["conversation-a"]
    assert harness.completion_attempts == ["conversation-a"]
    assert harness.fallback_calls == []
    assert harness.provider_dispatches == []

    harness.state["authority_available"] = True
    asyncio.run(harness.cleanup())
    assert harness.provider_dispatches == ["conversation-a"]
    assert harness.predecessor["status"] == "completed"


def test_pusher_processing_request_settles_every_coalesced_waiter():
    class ConsentRejected(RuntimeError):
        def __init__(self, *, retryable: bool):
            self.retryable = retryable

    async def exercise(mode: str, expected: str, *, retryable: bool = False):
        queued = asyncio.Event()
        release = asyncio.Event()
        pending = {}
        provider_calls = []

        async def send_pusher_payload(_payload):
            queued.set()
            await release.wait()
            if mode == "consent":
                raise ConsentRejected(retryable=retryable)
            provider_calls.append(mode)
            if mode == "transport_error":
                raise ConnectionError("pusher unavailable")

        request_processing = _nested_function(
            "routers/transcribe.py",
            "request_conversation_processing",
            {
                "AiConsentWebSocketRejected": ConsentRejected,
                "asyncio": asyncio,
                "bytes": bytes,
                "json": json,
                "PUSHER_PROCESSING_RESPONSE_TIMEOUT_SECONDS": 0.01,
                "struct": struct,
            },
            {
                "language": "en",
                "pending_conversation_requests": pending,
                "pending_request_event": asyncio.Event(),
                "pusher_connected": True,
                "pusher_ws": object(),
                "send_pusher_payload": send_pusher_payload,
                "session_id": "socket-a",
                "uid": "uid-a",
                "websocket_active": False,
            },
        )
        leader = asyncio.create_task(request_processing("conversation-a"))
        await queued.wait()
        follower = asyncio.create_task(request_processing("conversation-a"))
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.wait_for(asyncio.gather(leader, follower), timeout=1.0)

        assert results == [expected, expected]
        assert pending == {}
        if mode == "consent":
            assert provider_calls == []

    async def scenario():
        await exercise("consent", "consent_deferred", retryable=True)
        await exercise("consent", "consent_required", retryable=False)
        await exercise("transport_error", "unavailable")
        await exercise("timeout", "unavailable")

    asyncio.run(scenario())


def test_pusher_processing_request_waits_for_terminal_response():
    async def send_with_consent(consent_guard, provider_send, payload):
        await consent_guard()
        await provider_send(payload)

    class PusherSocket:
        def __init__(self):
            self.sent = []
            self.responses = asyncio.Queue()

        async def send(self, payload):
            self.sent.append(bytes(payload))

        async def recv(self):
            return await self.responses.get()

        async def close(self, *_args):
            return None

    socket = PusherSocket()
    processed = []

    async def connect_to_pusher(*_args, **_kwargs):
        return socket

    handler = _nested_function(
        "routers/transcribe.py",
        "create_pusher_task_handler",
        {
            "asyncio": asyncio,
            "struct": struct,
            "json": json,
            "time": time,
            "List": List,
            "ConnectionClosed": ConnectionError,
            "PusherTranscriptBatch": object,
            "AiConsentWebSocketRejected": RuntimeError,
            "_send_pusher_payload_with_current_consent": send_with_consent,
            "connect_to_trigger_pusher": connect_to_pusher,
            "deliver_all_pusher_transcript_batches": lambda *_args: asyncio.sleep(0, result=0),
            "get_audio_bytes_webhook_seconds": lambda _uid: 0,
            "is_audio_bytes_app_enabled": lambda _uid: False,
            "queue_pusher_transcript_batch": lambda *_args: None,
            "PUSHER_PROCESSING_RESPONSE_TIMEOUT_SECONDS": 1.0,
        },
        {
            "current_conversation_id": "conversation-a",
            "language": "en",
            "on_conversation_processed": processed.append,
            "private_cloud_sync_enabled": False,
            "sample_rate": 8_000,
            "session_id": "socket-a",
            "uid": "uid-a",
            "websocket_active": True,
        },
        argdefs=(None,),
    )
    consent_guard = lambda **_kwargs: asyncio.sleep(0)
    connect, _close, *_unused, request_processing, receive, _connected, _speaker = handler(consent_guard)

    async def scenario():
        await connect()
        receive_task = asyncio.create_task(receive())
        request_task = asyncio.create_task(request_processing("conversation-a"))
        while not socket.sent:
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert request_task.done() is False
        payload = bytearray(struct.pack("I", 201))
        payload.extend(json.dumps({"conversation_id": "conversation-a", "success": True}).encode())
        await socket.responses.put(bytes(payload))
        assert await request_task == "processed"

        failed_task = asyncio.create_task(request_processing("conversation-b"))
        while len(socket.sent) < 2:
            await asyncio.sleep(0)
        failed_payload = bytearray(struct.pack("I", 201))
        failed_payload.extend(
            json.dumps({"conversation_id": "conversation-b", "error": "stock_summary_transcript_changed"}).encode()
        )
        await socket.responses.put(bytes(failed_payload))
        assert await failed_task == "terminal_error"
        receive_task.cancel()
        await asyncio.gather(receive_task, return_exceptions=True)

    asyncio.run(scenario())
    assert processed == ["conversation-a"]


def test_capture_commit_is_fenced_after_reconnect_transfers_socket_ownership(monkeypatch):
    redis_db = _load_ownership_redis_module()
    conversations = _load_conversations_module()
    redis_db.set_in_progress_conversation_id("uid-a", "conversation-a", owner_id="socket-old")
    assert redis_db.claim_in_progress_conversation_id("uid-a", "conversation-a", "socket-new")
    monkeypatch.setattr(conversations, "redis_db", redis_db, raising=False)

    segment = {
        "id": "segment-late",
        "text": "late old socket capture",
        "speaker": "SPEAKER_00",
        "is_user": True,
        "start": 1.0,
        "end": 2.0,
    }

    class Ref:
        id = "batch-late"

        def __init__(self, data):
            self.data = data

        def get(self, transaction=None):
            return SimpleNamespace(exists=True, to_dict=lambda: self.data)

    class Transaction:
        def __init__(self):
            self.updates = []
            self.deletes = []

        def update(self, ref, payload):
            self.updates.append((ref, payload))

        def delete(self, ref):
            self.deletes.append(ref)

    transaction = Transaction()
    conversation_ref = Ref(
        {
            "id": "conversation-a",
            "capture_owner_id": "socket-new",
            "status": "in_progress",
            "data_protection_level": "standard",
            "transcript_segments": [],
        }
    )
    batch_ref = Ref({"batch_id": "batch-late", "payload": "encrypted"})
    monkeypatch.setattr(
        conversations,
        "_decode_capture_persistence_batch",
        lambda _uid, _data: {
            "conversation_id": "conversation-a",
            "segments": [segment],
            "photos": [],
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "capture_owner_id": "socket-old",
        },
    )
    monkeypatch.setattr(conversations, "_prepare_conversation_for_read", lambda data, _uid: data)
    monkeypatch.setattr(conversations, "_prepare_conversation_for_write", lambda data, _uid, _level: data)

    result = conversations._commit_capture_persistence_batch_transaction(
        transaction,
        conversation_ref,
        batch_ref,
        "uid-a",
        "conversation-a",
    )

    assert result["status"] == "ownership_lost"
    assert transaction.updates == []
    assert transaction.deletes == []


def test_reconnect_keeps_polling_until_a_late_superseded_socket_batch_is_committed():
    pending_batch_ids = []
    committed_batch_ids = []

    conversations_db = SimpleNamespace(
        list_capture_persistence_batches=lambda _uid, _conversation_id: list(pending_batch_ids),
        commit_capture_persistence_batch=lambda _uid, _conversation_id, batch_id, _owner_id, _generation: (
            committed_batch_ids.append(batch_id) or {"status": "committed"}
        ),
    )
    poll = _nested_function(
        "routers/transcribe.py",
        "poll_capture_persistence_batches",
        {"conversations_db": conversations_db},
        {},
    )
    should_keep_polling = _nested_function(
        "routers/transcribe.py",
        "should_keep_capture_recovery_polling",
        {},
        {},
    )

    recovery_conversation_ids = {"conversation-a"}
    recovered_count, still_owned = poll("uid-a", "conversation-a", "socket-new", "generation-new")
    assert (recovered_count, still_owned) == (0, True)
    assert should_keep_polling("conversation-a", "conversation-a", True)
    assert recovery_conversation_ids == {"conversation-a"}

    pending_batch_ids.append("batch-from-old-socket")
    recovered_count, still_owned = poll("uid-a", "conversation-a", "socket-new", "generation-new")
    assert (recovered_count, still_owned) == (1, True)
    assert committed_batch_ids == ["batch-from-old-socket"]

    assert not should_keep_polling("conversation-a", "conversation-b", True)
    recovery_conversation_ids.discard("conversation-a")
    assert recovery_conversation_ids == set()


def test_superseded_socket_cannot_persist_a_batch_after_ownership_handoff(monkeypatch):
    conversations = _load_conversations_module()
    calls = []

    monkeypatch.setattr(
        conversations.redis_db,
        "acquire_capture_commit_lease",
        lambda uid, conversation_id, owner_id: calls.append(("acquire", uid, conversation_id, owner_id)) or False,
    )
    monkeypatch.setattr(
        conversations,
        "persist_capture_persistence_batch",
        lambda *_args, **_kwargs: calls.append(("persist",)) or "batch-late",
    )

    result = conversations.persist_and_commit_capture_persistence_batch(
        "uid-a",
        "conversation-a",
        [{"id": "segment-late"}],
        datetime.now(timezone.utc),
        "socket-old",
    )

    assert result == {"status": "ownership_lost", "updated_segments": [], "removed_ids": []}
    assert calls == [("acquire", "uid-a", "conversation-a", "socket-old")]


def test_live_capture_persistence_holds_ownership_lease_through_commit(monkeypatch):
    conversations = _load_conversations_module()
    calls = []

    monkeypatch.setattr(
        conversations.redis_db,
        "acquire_capture_commit_lease",
        lambda uid, conversation_id, owner_id: calls.append(("acquire", uid, conversation_id, owner_id)) or True,
    )
    monkeypatch.setattr(
        conversations.redis_db,
        "release_capture_commit_lease",
        lambda uid, owner_id: calls.append(("release", uid, owner_id)) or True,
    )
    monkeypatch.setattr(
        conversations,
        "persist_capture_persistence_batch",
        lambda *_args, **_kwargs: calls.append(("persist",)) or "batch-live",
    )
    monkeypatch.setattr(
        conversations,
        "_commit_capture_persistence_batch",
        lambda *_args, **_kwargs: calls.append(("commit",))
        or {"status": "committed", "updated_segments": [], "removed_ids": []},
    )

    result = conversations.persist_and_commit_capture_persistence_batch(
        "uid-a",
        "conversation-a",
        [{"id": "segment-live"}],
        datetime.now(timezone.utc),
        "socket-current",
    )

    assert result["status"] == "committed"
    assert calls == [
        ("acquire", "uid-a", "conversation-a", "socket-current"),
        ("persist",),
        ("commit",),
        ("release", "uid-a", "socket-current"),
    ]


def test_capture_owner_is_initialized_before_reconnect_preparation_uses_it():
    source = (BACKEND / "routers" / "transcribe.py").read_text()

    initialization = source.index("capture_recovery_conversation_ids: set[str] = set()")
    preparation_call = source.index("timed_out_conversation_id = await _prepare_in_progess_conversations()")
    recovery_update = source.index("capture_recovery_conversation_ids.update(", preparation_call)

    assert initialization < preparation_call < recovery_update

    class StubConversation:
        def __init__(self, **kwargs):
            self.values = kwargs

        def dict(self):
            return dict(self.values)

    abandoned = []
    upserted = []

    class AdoptingRedis:
        active_id = "stale"

        def replace_stale_in_progress_conversation_id(self, _uid, _stale_id, new_id, _owner_id):
            self.active_id = new_id
            return False

        def get_in_progress_conversation_id(self, _uid):
            return self.active_id

        def remove_conversation_meeting_id(self, _conversation_id):
            raise AssertionError("an adopted stub must retain its meeting association")

    async def publish_capture_protocol_ready(*_args, **_kwargs):
        return True

    create_stub = _nested_function(
        "routers/transcribe.py",
        "_create_new_in_progress_conversation",
        {
            "Conversation": StubConversation,
            "ConversationSource": SimpleNamespace(omi="omi", desktop="desktop"),
            "ConversationStatus": SimpleNamespace(in_progress="in_progress"),
            "Structured": dict,
            "calendar_db": SimpleNamespace(get_meetings_in_time_range=lambda *_args: []),
            "conversations_db": SimpleNamespace(
                upsert_conversation=lambda _uid, conversation_data: upserted.append(conversation_data),
                abandon_capture_conversation_if_owned=lambda _uid, conversation_id, owner_id: abandoned.append(
                    (conversation_id, owner_id)
                )
                or False,
            ),
            "datetime": datetime,
            "redis_db": AdoptingRedis(),
            "timedelta": timedelta,
            "timezone": timezone,
            "uuid": SimpleNamespace(uuid4=lambda: "stub-adopted"),
        },
        {
            "_latency_log": lambda *_args, **_kwargs: None,
            "_publish_capture_protocol_ready": publish_capture_protocol_ready,
            "current_conversation_id": "stale",
            "language": "en",
            "private_cloud_sync_enabled": False,
            "session_id": "socket-a",
            "source": None,
            "uid": "uid-a",
            "websocket_active": True,
        },
    )

    assert (
        asyncio.run(
            create_stub(
                expected_conversation_id=None,
                expected_owner_id=None,
                replace_stale_conversation_id="stale",
                new_owner_id="socket-a",
                adopt=True,
            )
        )
        is False
    )
    assert upserted[0]["id"] == "stub-adopted"
    assert abandoned == [("stub-adopted", "socket-a")]


def test_production_reconnect_path_does_not_let_overlapping_socket_steal_authority():
    capture_protocol = _load_capture_protocol_module()
    now = datetime.now(timezone.utc)

    class Document:
        def __init__(self, data):
            self.data = data

        def get(self, transaction=None):
            return SimpleNamespace(exists=self.data is not None, to_dict=lambda: dict(self.data or {}))

    class Transaction:
        def __init__(self):
            self.sets = []
            self.updates = []

        def set(self, ref, payload):
            self.sets.append((ref, payload))

        def update(self, ref, payload):
            self.updates.append((ref, payload))

        def apply(self):
            for ref, payload in self.sets:
                ref.data = dict(payload)
            for ref, payload in self.updates:
                ref.data.update(payload)

    authority_ref = Document(
        {
            "protocol_version": 2,
            "conversation_id": "conversation-a",
            "generation": "generation-a",
            "owner_token": "socket-a",
            "state": "active",
            "lease_expires_at": now - timedelta(seconds=1),
        }
    )
    conversation_ref = Document(
        {
            "id": "conversation-a",
            "status": "in_progress",
            "capture_owner_id": "socket-a",
            "capture_protocol_version": 2,
            "capture_generation": "generation-a",
            "capture_owner_token": "socket-a",
            "capture_state": "active",
            "capture_lease_expires_at": now - timedelta(seconds=1),
            "finished_at": now,
        }
    )

    def claim_capture_authority_for_reconnect(
        _uid,
        conversation_id,
        generation,
        expected_owner_token,
        owner_token,
    ):
        transaction = Transaction()
        claimed = capture_protocol._claim_reconnect_authority_transaction.to_wrap(
            transaction,
            authority_ref,
            conversation_ref,
            conversation_id,
            generation,
            expected_owner_token,
            owner_token,
            now,
        )
        if claimed:
            transaction.apply()
        return claimed

    class Redis:
        def __init__(self):
            self.claims = []

        def get_in_progress_conversation_id(self, _uid):
            return "conversation-a"

        def claim_in_progress_conversation_id(self, _uid, conversation_id, owner_token):
            self.claims.append((conversation_id, owner_token))
            return True

        def replace_stale_in_progress_conversation_id(self, *_args):
            raise AssertionError("the exact active conversation must be claimed in place")

    redis = Redis()
    ready_receipts = []

    def make_prepare(session_id, generation_id):
        async def publish_capture_protocol_ready(conversation_id, **_kwargs):
            ready_receipts.append((conversation_id, generation_id, session_id))
            return True

        return _nested_function(
            "routers/transcribe.py",
            "_prepare_in_progess_conversations",
            {
                "CaptureReconnectAuthorityBusy": CaptureReconnectAuthorityBusy,
                "asyncio": asyncio,
                "claim_capture_authority_for_reconnect": claim_capture_authority_for_reconnect,
                "conversations_db": SimpleNamespace(),
                "datetime": datetime,
                "drain_capture_persistence_batches": lambda *_args: None,
                "mark_capture_drained": lambda *_args: True,
                "redis_db": redis,
                "retrieve_in_progress_conversation": lambda _uid: dict(conversation_ref.data),
                "timezone": timezone,
            },
            {
                "_create_new_in_progress_conversation": lambda **_kwargs: (_ for _ in ()).throw(
                    AssertionError("a current conversation must not create a replacement")
                ),
                "_publish_capture_protocol_ready": publish_capture_protocol_ready,
                "capture_recovery_conversation_ids": set(),
                "conversation_creation_timeout": 120,
                "current_conversation_id": None,
                "generation_id": generation_id,
                "session_id": session_id,
                "uid": "uid-a",
                "websocket_active": True,
            },
        )

    first_prepare = make_prepare("socket-b", "generation-b")
    assert asyncio.run(first_prepare()) is None
    assert authority_ref.data["owner_token"] == "socket-b"
    assert conversation_ref.data["capture_owner_id"] == "socket-b"
    assert redis.claims == [("conversation-a", "socket-b")]
    assert ready_receipts == [("conversation-a", "generation-b", "socket-b")]

    overlapping_prepare = make_prepare("socket-c", "generation-c")
    try:
        asyncio.run(overlapping_prepare())
    except CaptureReconnectAuthorityBusy as exc:
        assert str(exc) == "active conversation ownership changed during reconnect"
    else:
        raise AssertionError("the overlapping production reconnect must fail closed")

    assert authority_ref.data["owner_token"] == "socket-b"
    assert conversation_ref.data["capture_owner_id"] == "socket-b"
    assert redis.claims == [("conversation-a", "socket-b")]
    assert ready_receipts == [("conversation-a", "generation-b", "socket-b")]


def test_undelivered_capture_ready_releases_only_its_exact_authority():
    calls = []

    class ReadyEvent:
        def __init__(self, **values):
            self.values = values

    class Redis:
        @staticmethod
        def release_owned_in_progress_conversation_id(uid, conversation_id, owner_token):
            calls.append(("release", uid, conversation_id, owner_token))
            return True

    async def send_ready(event):
        calls.append(("send", dict(event.values)))
        return False

    def install_authority(uid, conversation_id, generation, owner_token, **options):
        calls.append(("install", uid, conversation_id, generation, owner_token, options))
        return True

    def mark_drained(uid, conversation_id, generation, owner_token):
        calls.append(("drain", uid, conversation_id, generation, owner_token))
        return True

    latency = []
    publish_ready = _nested_function(
        "routers/transcribe.py",
        "_publish_capture_protocol_ready",
        {
            "CAPTURE_PROTOCOL_VERSION": 2,
            "MessageServiceStatusEvent": ReadyEvent,
            "install_capture_authority": install_authority,
            "mark_capture_drained": mark_drained,
            "redis_db": Redis(),
        },
        {
            "_asend_message_event": send_ready,
            "_latency_log": lambda event, **metadata: latency.append((event, metadata)),
            "generation_id": "generation-a",
            "owner_token": "socket-a",
            "uid": "uid-a",
            "websocket_active": True,
            "websocket_close_code": 1000,
        },
    )

    assert (
        asyncio.run(
            publish_ready(
                "conversation-a",
                expected_conversation_id=None,
                adopt=True,
            )
        )
        is False
    )
    assert [call[0] for call in calls] == ["install", "send", "drain", "release"]
    assert calls[-2] == ("drain", "uid-a", "conversation-a", "generation-a", "socket-a")
    assert calls[-1] == ("release", "uid-a", "conversation-a", "socket-a")
    assert latency == [
        (
            "capture_protocol_ready_delivery_failed",
            {
                "conversation_id": "conversation-a",
                "authority_drained": True,
                "redis_owner_released": True,
                "cleanup_error_class": None,
            },
        )
    ]
    closure = dict(zip(publish_ready.__code__.co_freevars, (cell.cell_contents for cell in publish_ready.__closure__)))
    assert closure["websocket_active"] is False
    assert closure["websocket_close_code"] == 1013


def test_undelivered_capture_ready_does_not_release_after_authority_drift():
    releases = []

    class ReadyEvent:
        def __init__(self, **values):
            self.values = values

    class Redis:
        @staticmethod
        def release_owned_in_progress_conversation_id(*args):
            releases.append(args)
            return True

    async def send_ready(_event):
        return False

    publish_ready = _nested_function(
        "routers/transcribe.py",
        "_publish_capture_protocol_ready",
        {
            "CAPTURE_PROTOCOL_VERSION": 2,
            "MessageServiceStatusEvent": ReadyEvent,
            "install_capture_authority": lambda *_args, **_kwargs: True,
            "mark_capture_drained": lambda *_args: False,
            "redis_db": Redis(),
        },
        {
            "_asend_message_event": send_ready,
            "_latency_log": lambda *_args, **_kwargs: None,
            "generation_id": "generation-a",
            "owner_token": "socket-a",
            "uid": "uid-a",
            "websocket_active": True,
            "websocket_close_code": 1000,
        },
    )

    assert (
        asyncio.run(
            publish_ready(
                "conversation-a",
                expected_conversation_id=None,
                adopt=False,
            )
        )
        is False
    )
    assert releases == []


def _ready_admission_harness(send_error=None):
    calls = []
    diagnostics = []
    socket = SimpleNamespace(client_state=WebSocketState.CONNECTED, application_state=WebSocketState.CONNECTED)

    async def send_json(_payload):
        calls.append(('send',))
        if send_error is not None:
            raise send_error

    async def close(**options):
        calls.append(('close', options['code']))

    socket.send_json = send_json
    socket.close = close

    class Conversation:
        def __init__(self, **values):
            self.values = values

        def dict(self):
            return dict(self.values)

    class ReadyEvent:
        event_type = 'service_status'

        def __init__(self, **values):
            self.__dict__.update(values)

        def to_json(self):
            return {'private_body': 'never-log-event-body', **self.__dict__}

    def record(name, result=True):
        def operation(*args, **kwargs):
            calls.append((name, args, kwargs))
            return result

        return operation

    redis = SimpleNamespace(
        get_in_progress_conversation_id=record('retrieve', ''),
        claim_in_progress_conversation_id=record('claim'),
        release_owned_in_progress_conversation_id=record('release'),
    )
    values = {
        '_latency_log': lambda *_args, **_kwargs: None,
        'capture_recovery_conversation_ids': set(),
        'conversation_creation_timeout': 120,
        'current_conversation_id': None,
        'delivery_correlation': '0123456789abcdef',
        'generation_id': 'generation-a',
        'language': 'en',
        'owner_token': 'private-owner-token',
        'private_cloud_sync_enabled': False,
        'session_id': 'private-session',
        'source': None,
        'uid': 'private-uid',
        'websocket': socket,
        'websocket_active': True,
        'websocket_close_code': 1000,
    }
    shared_cells = {name: _cell(value) for name, value in values.items()}
    globals_ = {
        'CAPTURE_PROTOCOL_VERSION': 2,
        'CaptureReconnectAuthorityBusy': CaptureReconnectAuthorityBusy,
        'Conversation': Conversation,
        'ConversationSource': SimpleNamespace(omi='omi', desktop='desktop'),
        'ConversationStatus': SimpleNamespace(in_progress='in_progress'),
        'MessageServiceStatusEvent': ReadyEvent,
        'Structured': dict,
        'WebSocketDisconnect': WebSocketDisconnect,
        'WebSocketState': WebSocketState,
        'asyncio': asyncio,
        'conversations_db': SimpleNamespace(upsert_conversation=record('upsert')),
        'datetime': datetime,
        'install_capture_authority': record('install'),
        'json': json,
        'mark_capture_drained': record('drain'),
        'print': lambda *args, **_kwargs: diagnostics.append(' '.join(str(arg) for arg in args)),
        'redis_db': redis,
        'retrieve_in_progress_conversation': lambda _uid: None,
        'timezone': timezone,
        'uuid': SimpleNamespace(uuid4=lambda: f'conversation-{sum(call[0] == "upsert" for call in calls)}'),
    }
    globals_['_capture_send_failure_diagnostic'] = _nested_function(
        'routers/transcribe.py', '_capture_send_failure_diagnostic', globals_, {}
    )
    functions = {}
    for name in (
        '_asend_message_event',
        '_publish_capture_protocol_ready',
        '_create_new_in_progress_conversation',
        '_prepare_in_progess_conversations',
    ):
        function = _nested_function('routers/transcribe.py', name, globals_, values, shared_cells=shared_cells)
        if name == '_publish_capture_protocol_ready':
            function.__kwdefaults__ = {'expected_conversation_id': None, 'adopt': False}
        if name == '_create_new_in_progress_conversation':
            function.__kwdefaults__ = {
                'expected_conversation_id': None,
                'expected_owner_id': None,
                'replace_stale_conversation_id': None,
                'new_owner_id': values['session_id'],
                'adopt': True,
            }
        functions[name] = function
        values[name] = function
        shared_cells[name] = _cell(function)

    async def admit_provider():
        # Execute the actual production pre-provider guard, not a test copy of its condition.
        module = ast.parse((BACKEND / 'routers/transcribe.py').read_text())
        handler = next(
            node for node in module.body if isinstance(node, ast.AsyncFunctionDef) and node.name == '_stream_handler'
        )
        guard = next(
            node
            for node in handler.body
            if isinstance(node, ast.If)
            and ast.unparse(node.test) == 'not websocket_active or websocket.client_state != WebSocketState.CONNECTED'
        )
        admission = ast.AsyncFunctionDef(
            name='admit',
            args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
            body=[guard, ast.parse("provider_dispatches.append('provider')").body[0]],
            decorator_list=[],
        )
        scope = {
            **globals_,
            **{name: cell.cell_contents for name, cell in shared_cells.items()},
            'provider_dispatches': [],
        }
        exec(
            compile(ast.fix_missing_locations(ast.Module(body=[admission], type_ignores=[])), 'admission', 'exec'),
            scope,
        )
        await scope['admit']()
        return scope['provider_dispatches']

    return SimpleNamespace(
        calls=calls, diagnostics=diagnostics, cells=shared_cells, functions=functions, admit=admit_provider
    )


def test_failed_ready_retires_composed_preparation_without_second_install_or_provider():
    for error in (
        WebSocketDisconnect(code=1006, reason='never-log-client-reason'),
        RuntimeError('never-log-error-text'),
    ):
        harness = _ready_admission_harness(error)
        assert asyncio.run(harness.functions['_prepare_in_progess_conversations']()) is None
        assert [call[0] for call in harness.calls] == [
            'retrieve',
            'upsert',
            'claim',
            'install',
            'send',
            'drain',
            'release',
        ]
        assert harness.calls[-2][1] == ('private-uid', 'conversation-0', 'generation-a', 'private-owner-token')
        assert harness.calls[-1][1] == ('private-uid', 'conversation-0', 'private-owner-token')
        assert harness.cells['websocket_active'].cell_contents is False
        assert harness.cells['websocket_close_code'].cell_contents == 1013
        assert asyncio.run(harness.admit()) == []
        assert harness.calls[-1] == ('close', 1013)
        transport = [line for line in harness.diagnostics if line.startswith('[CAPTURE-TRANSPORT]')]
        assert len(transport) == 1
        diagnostic = json.loads(transport[0].split(' ', 1)[1])
        assert diagnostic['message_type'] == 'service_status'
        assert diagnostic['status'] == 'capture_protocol_ready'
        assert diagnostic['exception_class'] == type(error).__name__
        assert diagnostic['correlation'] == '0123456789abcdef'
        for secret in (
            'private-uid',
            'private-session',
            'private-owner-token',
            'never-log-client-reason',
            'never-log-error-text',
            'never-log-event-body',
        ):
            assert secret not in transport[0]


def test_retired_admission_callbacks_do_not_reinstall_or_create_but_disconnected_successor_remains_allowed():
    harness = _ready_admission_harness()
    harness.cells['websocket_active'].cell_contents = False
    assert asyncio.run(harness.functions['_prepare_in_progess_conversations']()) is None
    assert asyncio.run(harness.functions['_publish_capture_protocol_ready']('stale')) is False
    assert asyncio.run(harness.functions['_create_new_in_progress_conversation']()) is False
    assert harness.calls == []
    assert (
        asyncio.run(harness.functions['_create_new_in_progress_conversation'](adopt=False, new_owner_id=None)) is True
    )
    assert [call[0] for call in harness.calls] == ['upsert', 'claim']


def test_ready_failure_on_timed_out_candidate_stops_replacement_preparation():
    harness = _ready_admission_harness(WebSocketDisconnect(code=1006))
    prepare = harness.functions['_prepare_in_progess_conversations']
    create = harness.functions['_create_new_in_progress_conversation']
    prepare.__globals__.update(
        {
            'retrieve_in_progress_conversation': lambda _uid: {
                'id': 'expired-candidate',
                'capture_owner_id': 'expired-owner',
                'finished_at': datetime.now(timezone.utc) - timedelta(minutes=5),
            },
            'claim_capture_authority_for_reconnect': lambda *_args: True,
            'drain_capture_persistence_batches': lambda *_args: None,
        }
    )
    create.__globals__['conversations_db'].transfer_capture_conversation_owner = lambda *_args: True
    create.__globals__['redis_db'].rotate_in_progress_conversation_id = lambda *_args: True
    assert asyncio.run(prepare()) is None
    assert [call[0] for call in harness.calls].count('upsert') == 1
    assert [call[0] for call in harness.calls].count('install') == 1
    assert [call[0] for call in harness.calls].count('drain') == 1
    assert [call[0] for call in harness.calls].count('release') == 1
    assert asyncio.run(harness.admit()) == []


def test_retirement_on_last_preparation_pass_does_not_report_ownership_busy():
    harness = _ready_admission_harness()
    attempts = []

    async def conflict_then_retire(**_kwargs):
        attempts.append(True)
        if len(attempts) == 3:
            harness.cells['websocket_active'].cell_contents = False
        return False

    harness.cells['_create_new_in_progress_conversation'].cell_contents = conflict_then_retire
    assert asyncio.run(harness.functions['_prepare_in_progess_conversations']()) is None
    assert len(attempts) == 3
    assert not any(call[0] == 'install' for call in harness.calls)
    assert asyncio.run(harness.admit()) == []


def test_successful_composed_ready_admits_provider_and_keeps_single_authority():
    harness = _ready_admission_harness()
    assert asyncio.run(harness.functions['_prepare_in_progess_conversations']()) is None
    assert [call[0] for call in harness.calls] == ['retrieve', 'upsert', 'claim', 'install', 'send']
    assert asyncio.run(harness.admit()) == ['provider']
    assert harness.cells['websocket_active'].cell_contents is True
    assert not any(line.startswith('[CAPTURE-TRANSPORT]') for line in harness.diagnostics)


def test_send_diagnostic_rejects_untrusted_types_status_codes_and_states():
    diagnostic = _nested_function(
        'routers/transcribe.py', '_capture_send_failure_diagnostic', {'WebSocketState': WebSocketState}, {}
    )
    error = type('never-log-private-class', (RuntimeError,), {})('never-log-error-text')
    error.code = 4999
    error.reason = 'never-log-client-reason'
    msg = SimpleNamespace(event_type='never-log-event', status='never-log-status')
    socket = SimpleNamespace(client_state='never-log-state', application_state='never-log-state')
    result = diagnostic(msg, error, socket, active=False)
    assert result == {
        'message_type': 'other',
        'status': None,
        'exception_class': 'OtherError',
        'close_code': None,
        'websocket_client_state': 'UNKNOWN',
        'websocket_application_state': 'UNKNOWN',
        'websocket_active': False,
    }
    assert 'never-log' not in json.dumps(result)


def test_reconnect_conflict_is_converted_to_typed_temporary_close():
    source = (BACKEND / "routers" / "transcribe.py").read_text()
    startup = source.split(
        "timed_out_conversation_id = await _prepare_in_progess_conversations()",
        maxsplit=1,
    )[
        1
    ].split("# STT", maxsplit=1)[0]

    assert "except CaptureReconnectAuthorityBusy:" in startup
    assert 'status="capture_reconnect_busy"' in startup
    assert "websocket_close_code = 1013" in startup
    assert "websocket_active = False" in startup


def test_production_reconnect_rotates_expired_drained_candidate_behind_terminal_authority():
    capture_protocol = _load_capture_protocol_module()
    conversations = _load_conversations_module()
    now = datetime.now(timezone.utc)

    class Document:
        def __init__(self, data):
            self.data = data
            self.id = str((data or {}).get("id") or "")

        def get(self, transaction=None):
            return SimpleNamespace(exists=self.data is not None, to_dict=lambda: dict(self.data or {}))

    class Transaction:
        def __init__(self):
            self.sets = []
            self.updates = []

        def set(self, ref, payload):
            self.sets.append((ref, payload))

        def update(self, ref, payload):
            self.updates.append((ref, payload))

        def apply(self):
            for ref, payload in self.sets:
                ref.data = dict(payload)
            for ref, payload in self.updates:
                ref.data.update(payload)

    completed_authority = {
        "protocol_version": 2,
        "conversation_id": "completed-capture",
        "generation": "completed-generation",
        "owner_token": "completed-owner",
        "state": "terminal",
        "lease_expires_at": now - timedelta(minutes=5),
    }
    authority_ref = Document(dict(completed_authority))
    documents = {
        "drained-capture": Document(
            {
                "id": "drained-capture",
                "status": "in_progress",
                "capture_owner_id": None,
                "capture_protocol_version": 2,
                "capture_generation": "drained-generation",
                "capture_owner_token": "drained-owner",
                "capture_state": "drained",
                "capture_lease_expires_at": now - timedelta(minutes=10),
                "finished_at": now - timedelta(minutes=3),
                "transcript_segments": [{"id": "segment-a", "text": "fixture-content"}],
                "photos": [],
            }
        )
    }
    preserved_segments = list(documents["drained-capture"].data["transcript_segments"])

    def claim_capture_authority_for_reconnect(
        _uid,
        conversation_id,
        generation,
        expected_owner_token,
        owner_token,
    ):
        transaction = Transaction()
        claimed = capture_protocol._claim_reconnect_authority_transaction.to_wrap(
            transaction,
            authority_ref,
            documents[conversation_id],
            conversation_id,
            generation,
            expected_owner_token,
            owner_token,
            now,
        )
        if claimed:
            transaction.apply()
        return claimed

    def install_capture_authority(
        _uid,
        conversation_id,
        generation,
        owner_token,
        *,
        expected_conversation_id=None,
        adopt=False,
    ):
        transaction = Transaction()
        installed = capture_protocol._install_authority_transaction.to_wrap(
            transaction,
            authority_ref,
            documents[conversation_id],
            conversation_id,
            generation,
            owner_token,
            now,
            expected_conversation_id,
            documents.get(expected_conversation_id),
            adopt,
        )
        if installed:
            transaction.apply()
        return installed

    def complete_rotated_capture(_uid, conversation_id, generation, owner_token):
        transaction = Transaction()
        completed = capture_protocol._complete_rotated_capture_transaction.to_wrap(
            transaction,
            documents[conversation_id],
            conversation_id,
            generation,
            owner_token,
            now,
        )
        if completed:
            transaction.apply()
        return completed

    class Redis:
        def __init__(self):
            self.active_id = ""
            self.owner_id = ""

        def get_in_progress_conversation_id(self, _uid):
            return self.active_id

        def claim_in_progress_conversation_id(self, _uid, conversation_id, owner_token):
            if self.active_id and self.active_id != conversation_id:
                return False
            self.active_id = conversation_id
            self.owner_id = owner_token
            return True

        def rotate_in_progress_conversation_id(
            self,
            _uid,
            expected_conversation_id,
            expected_owner_id,
            new_conversation_id,
            new_owner_id,
        ):
            if self.active_id != expected_conversation_id or self.owner_id != expected_owner_id:
                return False
            self.active_id = new_conversation_id
            self.owner_id = new_owner_id or ""
            return True

        def replace_stale_in_progress_conversation_id(self, *_args):
            raise AssertionError("the exact drained candidate must be claimed in place")

        def remove_conversation_meeting_id(self, *_args):
            raise AssertionError("the OMI capture path must not create a meeting binding")

    redis = Redis()
    deleted = []
    processed = []

    class Conversation:
        def __init__(self, **values):
            self.values = values

        def dict(self):
            return dict(self.values)

    class ConversationRepository:
        @staticmethod
        def upsert_conversation(_uid, conversation_data):
            documents[conversation_data["id"]] = Document(dict(conversation_data))

        @staticmethod
        def transfer_capture_conversation_owner(
            _uid,
            previous_conversation_id,
            expected_previous_owner_id,
            next_conversation_id,
            next_owner_id,
        ):
            transaction = Transaction()
            transferred = conversations._transfer_capture_conversation_owner_transaction(
                transaction,
                documents[previous_conversation_id],
                documents[next_conversation_id],
                expected_previous_owner_id,
                next_owner_id,
            )
            if transferred:
                transaction.apply()
            return transferred

        @staticmethod
        def get_conversation(_uid, conversation_id):
            return dict(documents[conversation_id].data)

        @staticmethod
        def delete_conversation(_uid, conversation_id, **_kwargs):
            deleted.append(conversation_id)
            documents.pop(conversation_id, None)

        @staticmethod
        def rollback_capture_conversation_owner_transfer(*_args):
            raise AssertionError("the successful rotation must not roll back")

        @staticmethod
        def abandon_capture_conversation_if_owned(*_args):
            raise AssertionError("the successful rotation must not abandon a conversation")

    repository = ConversationRepository()
    ready_receipts = []

    class ReadyEvent:
        def __init__(self, **values):
            self.values = values

    async def send_ready(event):
        ready_receipts.append(dict(event.values))
        return True

    publish_ready_impl = _nested_function(
        "routers/transcribe.py",
        "_publish_capture_protocol_ready",
        {
            "CAPTURE_PROTOCOL_VERSION": 2,
            "MessageServiceStatusEvent": ReadyEvent,
            "install_capture_authority": install_capture_authority,
        },
        {
            "_asend_message_event": send_ready,
            "_latency_log": lambda *_args, **_kwargs: None,
            "generation_id": "replacement-generation",
            "owner_token": "replacement-owner",
            "uid": "uid-a",
            "websocket_active": True,
            "websocket_close_code": 1000,
        },
    )

    async def publish_ready(conversation_id, *, expected_conversation_id=None, adopt=False):
        return await publish_ready_impl(
            conversation_id,
            expected_conversation_id=expected_conversation_id,
            adopt=adopt,
        )

    create_stub = _nested_function(
        "routers/transcribe.py",
        "_create_new_in_progress_conversation",
        {
            "Conversation": Conversation,
            "ConversationSource": SimpleNamespace(omi="omi", desktop="desktop"),
            "ConversationStatus": SimpleNamespace(in_progress="in_progress"),
            "Structured": dict,
            "calendar_db": SimpleNamespace(get_meetings_in_time_range=lambda *_args: []),
            "conversations_db": repository,
            "datetime": datetime,
            "redis_db": redis,
            "timedelta": timedelta,
            "timezone": timezone,
            "uuid": SimpleNamespace(uuid4=lambda: "fresh-capture"),
        },
        {
            "_latency_log": lambda *_args, **_kwargs: None,
            "_publish_capture_protocol_ready": publish_ready,
            "current_conversation_id": None,
            "language": "en",
            "private_cloud_sync_enabled": False,
            "session_id": "replacement-owner",
            "source": None,
            "uid": "uid-a",
            "websocket_active": True,
        },
    )

    async def create_new_in_progress_conversation(**kwargs):
        options = {
            "expected_conversation_id": None,
            "expected_owner_id": None,
            "replace_stale_conversation_id": None,
            "new_owner_id": "replacement-owner",
            "adopt": True,
        }
        options.update(kwargs)
        return await create_stub(**options)

    prepare = _nested_function(
        "routers/transcribe.py",
        "_prepare_in_progess_conversations",
        {
            "asyncio": asyncio,
            "claim_capture_authority_for_reconnect": claim_capture_authority_for_reconnect,
            "conversations_db": repository,
            "datetime": datetime,
            "drain_capture_persistence_batches": lambda *_args: None,
            "mark_capture_drained": lambda *_args: True,
            "redis_db": redis,
            "retrieve_in_progress_conversation": lambda _uid: dict(documents["drained-capture"].data),
            "timezone": timezone,
        },
        {
            "_create_new_in_progress_conversation": create_new_in_progress_conversation,
            "_publish_capture_protocol_ready": publish_ready,
            "capture_recovery_conversation_ids": set(),
            "conversation_creation_timeout": 120,
            "current_conversation_id": None,
            "generation_id": "replacement-generation",
            "session_id": "replacement-owner",
            "uid": "uid-a",
            "websocket_active": True,
        },
    )

    assert asyncio.run(prepare()) == "drained-capture"
    assert redis.active_id == "fresh-capture"
    assert redis.owner_id == "replacement-owner"
    assert ready_receipts == [
        {
            "status": "capture_protocol_ready",
            "protocol_version": 2,
            "conversation_id": "fresh-capture",
            "generation": "replacement-generation",
            "owner_token": "replacement-owner",
        }
    ]
    assert authority_ref.data["conversation_id"] == "fresh-capture"
    assert authority_ref.data["generation"] == "replacement-generation"
    assert authority_ref.data["state"] == "active"
    assert documents["fresh-capture"].data["capture_state"] == "active"
    assert documents["drained-capture"].data["capture_state"] == "drained"

    async def process_fallback(conversation):
        processed.append(conversation["id"])
        documents[conversation["id"]].data["status"] = "completed"

    async def buffers_drained(_conversation_id):
        return True

    async def unused_provider_processing(_conversation_id):
        raise AssertionError("local processing is required for this regression")

    process = _nested_function(
        "routers/transcribe.py",
        "_process_conversation",
        {
            "PUSHER_ENABLED": False,
            "complete_rotated_capture": complete_rotated_capture,
            "conversations_db": repository,
            "drain_capture_persistence_batches": lambda *_args: None,
        },
        {
            "_create_conversation_fallback": process_fallback,
            "_latency_log": lambda *_args, **_kwargs: None,
            "_wait_for_capture_buffers_to_drain": buffers_drained,
            "generation_id": "replacement-generation",
            "on_conversation_processing_started": lambda *_args: None,
            "owner_token": "replacement-owner",
            "request_conversation_processing": unused_provider_processing,
            "session_id": "replacement-owner",
            "uid": "uid-a",
        },
    )

    assert asyncio.run(process("drained-capture", wait_for_buffers=True)) is True
    assert processed == ["drained-capture"]
    assert deleted == []
    assert documents["drained-capture"].data["transcript_segments"] == preserved_segments
    assert documents["drained-capture"].data["status"] == "completed"
    assert documents["drained-capture"].data["capture_state"] == "terminal"
    assert authority_ref.data["conversation_id"] == "fresh-capture"
    assert completed_authority["conversation_id"] == "completed-capture"


def test_production_reconnect_adopts_owner_bound_legacy_successor_behind_expired_authority():
    capture_protocol = _load_capture_protocol_module()
    now = datetime.now(timezone.utc)

    class Document:
        def __init__(self, data):
            self.data = data

        def get(self, transaction=None):
            return SimpleNamespace(exists=self.data is not None, to_dict=lambda: dict(self.data or {}))

    class Transaction:
        def __init__(self):
            self.sets = []
            self.updates = []

        def set(self, ref, payload):
            self.sets.append((ref, payload))

        def update(self, ref, payload):
            self.updates.append((ref, payload))

        def apply(self):
            for ref, payload in self.sets:
                ref.data = dict(payload)
            for ref, payload in self.updates:
                ref.data.update(payload)

    authority_ref = Document(
        {
            "protocol_version": 2,
            "conversation_id": "prior-capture",
            "generation": "prior-generation",
            "owner_token": "prior-owner",
            "state": "active",
            "lease_expires_at": now - timedelta(minutes=5),
        }
    )
    documents = {
        "prior-capture": Document(
            {
                "id": "prior-capture",
                "status": "in_progress",
                "capture_owner_id": None,
                "capture_protocol_version": 2,
                "capture_generation": "prior-generation",
                "capture_owner_token": "prior-owner",
                "capture_state": "active",
                "capture_lease_expires_at": now - timedelta(minutes=5),
            }
        ),
        "legacy-successor": Document(
            {
                "id": "legacy-successor",
                "status": "in_progress",
                "capture_owner_id": "successor-owner",
                "capture_protocol_version": None,
                "capture_state": None,
                "finished_at": now,
                "transcript_segments": [{"id": "segment-a", "text": "preserved-content"}],
                "structured": {"title": "preserved-title"},
                "photos": ["preserved-photo"],
            }
        ),
    }
    preserved_content = {
        key: documents["legacy-successor"].data[key] for key in ("transcript_segments", "structured", "photos")
    }

    def claim_capture_authority_for_reconnect(
        _uid,
        conversation_id,
        generation,
        expected_owner_token,
        owner_token,
    ):
        transaction = Transaction()
        claimed = capture_protocol._claim_reconnect_authority_transaction.to_wrap(
            transaction,
            authority_ref,
            documents[conversation_id],
            conversation_id,
            generation,
            expected_owner_token,
            owner_token,
            now,
            lambda prior_conversation_id: documents[prior_conversation_id],
        )
        if claimed:
            transaction.apply()
        return claimed

    def install_capture_authority(
        _uid,
        conversation_id,
        generation,
        owner_token,
        *,
        expected_conversation_id=None,
        adopt=False,
    ):
        transaction = Transaction()
        installed = capture_protocol._install_authority_transaction.to_wrap(
            transaction,
            authority_ref,
            documents[conversation_id],
            conversation_id,
            generation,
            owner_token,
            now,
            expected_conversation_id,
            documents.get(expected_conversation_id),
            adopt,
        )
        if installed:
            transaction.apply()
        return installed

    class Redis:
        def __init__(self):
            self.active_id = ""
            self.owner_id = ""

        def get_in_progress_conversation_id(self, _uid):
            return self.active_id

        def claim_in_progress_conversation_id(self, _uid, conversation_id, owner_token):
            if self.active_id and self.active_id != conversation_id:
                return False
            self.active_id = conversation_id
            self.owner_id = owner_token
            return True

        def replace_stale_in_progress_conversation_id(self, *_args):
            raise AssertionError("the exact adopted candidate must be claimed in place")

    class ReadyEvent:
        def __init__(self, **values):
            self.values = values

    ready_receipts = []

    async def send_ready(event):
        ready_receipts.append(dict(event.values))
        return True

    publish_ready_impl = _nested_function(
        "routers/transcribe.py",
        "_publish_capture_protocol_ready",
        {
            "CAPTURE_PROTOCOL_VERSION": 2,
            "MessageServiceStatusEvent": ReadyEvent,
            "install_capture_authority": install_capture_authority,
        },
        {
            "_asend_message_event": send_ready,
            "_latency_log": lambda *_args, **_kwargs: None,
            "generation_id": "replacement-generation",
            "owner_token": "replacement-owner",
            "uid": "uid-a",
            "websocket_active": True,
            "websocket_close_code": 1000,
        },
    )

    async def publish_ready(conversation_id, *, expected_conversation_id=None, adopt=False):
        return await publish_ready_impl(
            conversation_id,
            expected_conversation_id=expected_conversation_id,
            adopt=adopt,
        )

    redis = Redis()
    prepare = _nested_function(
        "routers/transcribe.py",
        "_prepare_in_progess_conversations",
        {
            "asyncio": asyncio,
            "claim_capture_authority_for_reconnect": claim_capture_authority_for_reconnect,
            "conversations_db": SimpleNamespace(),
            "datetime": datetime,
            "drain_capture_persistence_batches": lambda *_args: None,
            "mark_capture_drained": lambda *_args: True,
            "redis_db": redis,
            "retrieve_in_progress_conversation": lambda _uid: dict(documents["legacy-successor"].data),
            "timezone": timezone,
        },
        {
            "_create_new_in_progress_conversation": lambda **_kwargs: (_ for _ in ()).throw(
                AssertionError("the recoverable candidate must not be replaced")
            ),
            "_publish_capture_protocol_ready": publish_ready,
            "capture_recovery_conversation_ids": set(),
            "conversation_creation_timeout": 120,
            "current_conversation_id": None,
            "generation_id": "replacement-generation",
            "session_id": "replacement-owner",
            "uid": "uid-a",
            "websocket_active": True,
        },
    )

    assert asyncio.run(prepare()) is None
    assert redis.active_id == "legacy-successor"
    assert redis.owner_id == "replacement-owner"
    assert authority_ref.data["conversation_id"] == "legacy-successor"
    assert authority_ref.data["generation"] == "replacement-generation"
    assert documents["legacy-successor"].data["capture_owner_id"] == "replacement-owner"
    assert documents["legacy-successor"].data["capture_state"] == "active"
    assert {key: documents["legacy-successor"].data[key] for key in preserved_content} == preserved_content
    assert ready_receipts == [
        {
            "status": "capture_protocol_ready",
            "protocol_version": 2,
            "conversation_id": "legacy-successor",
            "generation": "replacement-generation",
            "owner_token": "replacement-owner",
        }
    ]


def test_failed_stub_publication_abandons_only_the_still_owned_firestore_generation():
    conversations = _load_conversations_module()

    class Ref:
        def __init__(self, data):
            self.data = data

        def get(self, transaction=None):
            return SimpleNamespace(exists=True, to_dict=lambda: self.data)

    class Transaction:
        def __init__(self):
            self.updates = []

        def update(self, ref, payload):
            self.updates.append((ref, payload))

    owned_ref = Ref({"status": "in_progress", "capture_owner_id": "socket-old"})
    owned_transaction = Transaction()
    assert conversations._abandon_capture_conversation_if_owned_transaction(
        owned_transaction,
        owned_ref,
        "socket-old",
    )
    assert owned_transaction.updates[0][1]["status"] == "failed"
    assert owned_transaction.updates[0][1]["capture_owner_id"] is None

    adopted_ref = Ref({"status": "in_progress", "capture_owner_id": "socket-new"})
    adopted_transaction = Transaction()
    assert not conversations._abandon_capture_conversation_if_owned_transaction(
        adopted_transaction,
        adopted_ref,
        "socket-old",
    )
    assert adopted_transaction.updates == []


def test_duplicate_processing_claim_is_a_no_write_inflight_result():
    conversations = _load_conversations_module()

    class Ref:
        def get(self, transaction=None):
            return SimpleNamespace(
                exists=True,
                to_dict=lambda: {
                    "status": "processing",
                    "initial_processing_claimed_at": datetime.now(timezone.utc),
                },
            )

    class Transaction:
        def update(self, *_args):
            raise AssertionError("an inflight processor must retain exclusive processing authority")

    result = conversations._claim_initial_conversation_processing_transaction(Transaction(), Ref())

    assert result == {"status": "processing_in_progress"}


def test_stale_processing_claim_without_a_lease_can_be_recovered():
    conversations = _load_conversations_module()

    class Ref:
        def get(self, transaction=None):
            return SimpleNamespace(exists=True, to_dict=lambda: {"status": "processing"})

    class Transaction:
        def __init__(self):
            self.updates = []

        def update(self, _ref, payload):
            self.updates.append(payload)

    transaction = Transaction()
    result = conversations._claim_initial_conversation_processing_transaction(transaction, Ref())

    assert result["status"] == "processing_claimed"
    assert result["claim_token"]
    assert transaction.updates[0]["status"] == "processing"
    assert isinstance(transaction.updates[0]["initial_processing_claimed_at"], datetime)
    assert transaction.updates[0]["initial_processing_claim_token"] == result["claim_token"]


def test_reconnect_claims_firestore_authority_before_publishing_redis_owner():
    source = (BACKEND / "routers" / "transcribe.py").read_text()
    preparation = source.split("async def _prepare_in_progess_conversations():", maxsplit=1)[1].split(
        "timed_out_conversation_id = await _prepare_in_progess_conversations()", maxsplit=1
    )[0]

    assert preparation.index("claim_capture_authority_for_reconnect(") < preparation.index(
        "claim_in_progress_conversation_id("
    )
    assert "rebind_capture_conversation_owner(" not in preparation
    assert "conversations_db.bind_capture_conversation_owner(" not in preparation


def test_capture_owner_rebind_and_rollback_are_generation_conditional():
    conversations = _load_conversations_module()

    class Ref:
        def __init__(self, owner_id):
            self.data = {"status": "in_progress", "capture_owner_id": owner_id}

        def get(self, transaction=None):
            return SimpleNamespace(exists=True, to_dict=lambda: dict(self.data))

    class Transaction:
        def __init__(self):
            self.updates = []

        def update(self, ref, payload):
            self.updates.append(payload)
            ref.data.update(payload)

    ref = Ref("socket-a")
    takeover = Transaction()
    assert conversations._rebind_capture_conversation_owner_transaction(
        takeover,
        ref,
        "socket-a",
        "socket-b",
    )
    assert ref.data["capture_owner_id"] == "socket-b"

    stale_rollback = Transaction()
    ref.data["capture_owner_id"] = "socket-c"
    assert not conversations._rebind_capture_conversation_owner_transaction(
        stale_rollback,
        ref,
        "socket-b",
        "socket-a",
    )
    assert stale_rollback.updates == []
    assert ref.data["capture_owner_id"] == "socket-c"


def test_capture_photo_commit_uses_the_same_durable_owner_fence(monkeypatch):
    conversations = _load_conversations_module()
    photo = {"id": "photo-a", "base64": "synthetic"}

    class Snapshot:
        exists = True

        def __init__(self, data):
            self.data = data

        def to_dict(self):
            return self.data

    class ChildRef:
        def __init__(self, ref_id):
            self.id = ref_id

    class Collection:
        def document(self, ref_id):
            return ChildRef(ref_id)

    class Ref:
        id = "ref"

        def __init__(self, data):
            self.data = data

        def get(self, transaction=None):
            return Snapshot(self.data)

        def collection(self, name):
            assert name == "photos"
            return Collection()

    class Transaction:
        def __init__(self):
            self.updates = []
            self.sets = []
            self.deletes = []

        def update(self, ref, payload):
            self.updates.append((ref, payload))

        def set(self, ref, payload):
            self.sets.append((ref, payload))

        def delete(self, ref):
            self.deletes.append(ref)

    conversation_ref = Ref(
        {
            "id": "conversation-a",
            "capture_owner_id": "socket-current",
            "status": "in_progress",
            "data_protection_level": "standard",
            "transcript_segments": [],
        }
    )
    batch_ref = Ref({"batch_id": "batch-photo", "payload": "encrypted"})
    batch_ref.id = "batch-photo"
    monkeypatch.setattr(
        conversations,
        "_decode_capture_persistence_batch",
        lambda _uid, _data: {
            "conversation_id": "conversation-a",
            "segments": [],
            "photos": [photo],
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "capture_owner_id": "socket-old",
        },
    )
    monkeypatch.setattr(conversations, "_prepare_conversation_for_read", lambda data, _uid: data)
    monkeypatch.setattr(conversations, "_prepare_conversation_for_write", lambda data, _uid, _level: data)
    monkeypatch.setattr(conversations, "_prepare_photo_for_write", lambda data, _uid, _level: data)

    stale_transaction = Transaction()
    stale_result = conversations._commit_capture_persistence_batch_transaction(
        stale_transaction,
        conversation_ref,
        batch_ref,
        "uid-a",
        "conversation-a",
        "socket-old",
    )
    assert stale_result["status"] == "ownership_lost"
    assert stale_transaction.updates == []
    assert stale_transaction.sets == []

    current_transaction = Transaction()
    current_result = conversations._commit_capture_persistence_batch_transaction(
        current_transaction,
        conversation_ref,
        batch_ref,
        "uid-a",
        "conversation-a",
        "socket-current",
    )
    assert current_result["status"] == "committed"
    assert current_transaction.sets[0][0].id == "photo-a"
    assert current_transaction.updates[0][1]["source"] == "openglass"
    assert current_transaction.deletes == [batch_ref]


def test_capture_owner_transfer_fences_the_previous_firestore_generation():
    conversations = _load_conversations_module()

    class Ref:
        def __init__(self, ref_id, data):
            self.id = ref_id
            self.data = data

        def get(self, transaction=None):
            return SimpleNamespace(exists=True, to_dict=lambda: self.data)

    class Transaction:
        def __init__(self):
            self.updates = []

        def update(self, ref, payload):
            self.updates.append((ref.id, payload))

        def apply(self, refs):
            for ref_id, payload in self.updates:
                refs[ref_id].data.update(payload)

    transaction = Transaction()
    refs = {
        "old": Ref("old", {"status": "in_progress", "capture_owner_id": "socket-old"}),
        "new": Ref("new", {"status": "in_progress", "capture_owner_id": "socket-new"}),
    }
    transferred = conversations._transfer_capture_conversation_owner_transaction(
        transaction,
        refs["old"],
        refs["new"],
        "socket-old",
        "socket-new",
    )

    assert transferred is True
    assert transaction.updates[0][0] == "old"
    predecessor_update = transaction.updates[0][1]
    assert predecessor_update["capture_owner_id"] is None
    assert predecessor_update["status"] == "processing"
    assert isinstance(predecessor_update["initial_processing_claimed_at"], datetime)
    assert predecessor_update["initial_processing_claim_token"] == conversations.CAPTURE_ROTATION_PROCESSING_CLAIM_TOKEN
    assert predecessor_update["initial_processing_release_token"] is None
    assert predecessor_update["capture_rotation_successor_id"] == "new"
    assert transaction.updates[1] == ("new", {"capture_owner_id": "socket-new"})

    transaction.apply(refs)
    claim_transaction = Transaction()
    assert conversations._claim_initial_conversation_processing_transaction(
        claim_transaction,
        refs["old"],
    ) == {"status": "processing_in_progress"}
    assert claim_transaction.updates == []

    activation_transaction = Transaction()
    assert conversations._activate_capture_conversation_processing_transaction(
        activation_transaction,
        refs["old"],
        "new",
    )
    assert activation_transaction.updates == [
        (
            "old",
            {
                "initial_processing_claimed_at": None,
                "initial_processing_claim_token": None,
                "capture_rotation_successor_id": None,
            },
        )
    ]


def test_capture_owner_transfer_rollback_restores_only_its_rotation_reservation():
    conversations = _load_conversations_module()

    class Ref:
        def __init__(self, ref_id, data):
            self.id = ref_id
            self.data = data

        def get(self, transaction=None):
            return SimpleNamespace(exists=True, to_dict=lambda: self.data)

    class Transaction:
        def __init__(self):
            self.updates = []
            self.deletes = []

        def update(self, ref, payload):
            self.updates.append((ref.id, payload))

        def delete(self, ref):
            self.deletes.append(ref.id)

    previous = Ref(
        "old",
        {
            "status": "processing",
            "capture_owner_id": None,
            "initial_processing_claimed_at": datetime.now(timezone.utc),
            "initial_processing_claim_token": conversations.CAPTURE_ROTATION_PROCESSING_CLAIM_TOKEN,
            "capture_rotation_successor_id": "new",
        },
    )
    successor = Ref("new", {"status": "in_progress", "capture_owner_id": "socket-new"})
    transaction = Transaction()

    rolled_back = conversations._rollback_capture_conversation_owner_transfer_transaction(
        transaction,
        previous,
        successor,
        "socket-old",
        "socket-new",
    )

    assert rolled_back is True
    assert transaction.updates == [
        (
            "old",
            {
                "capture_owner_id": "socket-old",
                "status": "in_progress",
                "initial_processing_claimed_at": None,
                "initial_processing_claim_token": None,
                "capture_rotation_successor_id": None,
            },
        ),
    ]
    assert transaction.deletes == ["new"]

    previous.data["initial_processing_claim_token"] = "processing-claim"
    claimed_transaction = Transaction()
    assert (
        conversations._rollback_capture_conversation_owner_transfer_transaction(
            claimed_transaction,
            previous,
            successor,
            "socket-old",
            "socket-new",
        )
        is False
    )
    assert claimed_transaction.updates == []
    assert claimed_transaction.deletes == []


def test_ownership_loss_exits_old_stream_and_photos_have_no_unfenced_write_path():
    source = (BACKEND / "routers" / "transcribe.py").read_text()
    stream_source = source.split("async def stream_transcript_process():", maxsplit=1)[1].split(
        "async def conversation_timeout_task():", maxsplit=1
    )[0]

    assert 'phase="recovery"' in stream_source
    assert 'phase="live"' in stream_source
    assert stream_source.count("websocket_active = False") >= 2
    assert "store_conversation_photos" not in stream_source
    assert "photos=photos_to_process" in stream_source


def test_incident_regression_file_triggers_pull_request_and_push_ci():
    workflow = (BACKEND.parent / ".github" / "workflows" / "hermes-cloud-runtime-tests.yml").read_text()

    assert workflow.count('- "backend/tests/unit/test_capture_incident_1210_regressions.py"') == 2


def test_stock_summary_commit_rejects_transcript_appended_after_processing_snapshot(monkeypatch):
    conversations = _load_conversations_module()
    transcript_snapshot = [{"id": "segment-a", "text": "source transcript"}]
    durable_transcript = [
        *transcript_snapshot,
        {"id": "segment-b", "text": "capture appended during summarization"},
    ]

    class ConversationRef:
        def __init__(self):
            self.data = {
                "id": "conversation-a",
                "created_at": datetime(2026, 8, 13, tzinfo=timezone.utc),
                "structured": {},
                "summary_versions": [],
                "active_summary_version_id": None,
                "transcript_segments": durable_transcript,
                "status": "processing",
                "discarded": False,
                "data_protection_level": "standard",
            }

        def get(self, transaction=None):
            return SimpleNamespace(exists=True, to_dict=lambda: self.data)

    class Transaction:
        def __init__(self):
            self.updates = []

        def update(self, ref, payload):
            self.updates.append((ref, payload))

    processing_snapshot = {
        "id": "conversation-a",
        "created_at": datetime(2026, 8, 13, tzinfo=timezone.utc),
        "structured": {
            "title": "Summary of segment A",
            "overview": "Generated before segment B arrived.",
            "emoji": "brain",
            "category": "other",
        },
        "summary_versions": [],
        "active_summary_version_id": None,
        "transcript_segments": transcript_snapshot,
        "status": "completed",
        "discarded": False,
        "data_protection_level": "standard",
    }
    transaction = Transaction()
    monkeypatch.setattr(conversations, "_prepare_conversation_for_write", lambda data, _uid, _level: data)

    result = conversations._commit_stock_summary_processing_result_transaction(
        transaction,
        ConversationRef(),
        "uid-a",
        processing_snapshot,
        expected_active_summary_version_id=None,
        expected_transcript_hash=conversations.transcript_grounding_hash(transcript_snapshot),
    )

    assert result["status"] == conversations.conversation_stock_summary_transcript_changed
    assert transaction.updates == []
