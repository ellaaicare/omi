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


def test_reconnect_rebinds_firestore_before_publishing_redis_owner():
    source = (BACKEND / "routers" / "transcribe.py").read_text()
    preparation = source.split("async def _prepare_in_progess_conversations():", maxsplit=1)[1].split(
        "timed_out_conversation_id = await _prepare_in_progess_conversations()", maxsplit=1
    )[0]

    assert preparation.index("rebind_capture_conversation_owner(") < preparation.index(
        "claim_in_progress_conversation_id("
    )
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

    transaction = Transaction()
    transferred = conversations._transfer_capture_conversation_owner_transaction(
        transaction,
        Ref("old", {"status": "in_progress", "capture_owner_id": "socket-old"}),
        Ref("new", {"status": "in_progress", "capture_owner_id": "socket-new"}),
        "socket-old",
        "socket-new",
    )

    assert transferred is True
    assert transaction.updates == [
        ("old", {"capture_owner_id": None}),
        ("new", {"capture_owner_id": "socket-new"}),
    ]


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
