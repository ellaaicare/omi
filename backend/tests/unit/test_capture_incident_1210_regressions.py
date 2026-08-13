import asyncio
import importlib.util
import json
import struct
import sys
import time
import types
from datetime import datetime, timezone
from functools import cache
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import List
from unittest.mock import MagicMock, patch

BACKEND = Path(__file__).resolve().parents[2]


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


def _nested_function(relative_path: str, name: str, globals_: dict, closure_values: dict):
    code = _nested_code(relative_path, name)
    missing = set(code.co_freevars) - set(closure_values)
    assert not missing, f"missing closure values for {name}: {sorted(missing)}"
    closure = tuple(_cell(closure_values[freevar]) for freevar in code.co_freevars)
    return types.FunctionType(code, {"__builtins__": __builtins__, **globals_}, name, closure=closure)


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
    )
    connect, close, *_unused, request_processing, _receive, _connected, _speaker = handler()
    fallback_calls = []

    async def fallback(conversation):
        fallback_calls.append(conversation["id"])

    process = _nested_function(
        "routers/transcribe.py",
        "_process_conversation",
        {
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
            "on_conversation_processing_started": lambda _conversation_id: None,
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


def test_pusher_processing_request_waits_for_terminal_response():
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
    )
    connect, _close, *_unused, request_processing, receive, _connected, _speaker = handler()

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
        assert await request_task is True
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

    assert result["status"] != "committed"
    assert transaction.updates == []
    assert transaction.deletes == []


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

    assert result["status"] != "committed"
    assert transaction.updates == []
