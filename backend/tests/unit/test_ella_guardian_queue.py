import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

sys.modules.setdefault("asyncpg", MagicMock())
multipart_module = types.ModuleType("multipart")
multipart_module.__version__ = "0.0-test"
multipart_submodule = types.ModuleType("multipart.multipart")
multipart_submodule.parse_options_header = lambda value: (value, {})
sys.modules["multipart"] = multipart_module
sys.modules["multipart.multipart"] = multipart_submodule

ella_module = types.ModuleType("ella")
routers_module = types.ModuleType("ella.routers")
resolve_module = types.ModuleType("ella.routers.resolve")


async def _fake_resolve_user_routing(uid):
    return {"routing": {"agentId": f"ella-{uid}"}}


resolve_module.resolve_user_routing = _fake_resolve_user_routing
sys.modules["ella"] = ella_module
sys.modules["ella.routers"] = routers_module
sys.modules["ella.routers.resolve"] = resolve_module

_backend_path = Path(__file__).resolve().parents[2]
if str(_backend_path) not in sys.path:
    sys.path.insert(0, str(_backend_path))

_module_path = _backend_path / "ella" / "routers" / "guardian.py"
_spec = importlib.util.spec_from_file_location("ella_guardian_test_module", _module_path)
guardian = importlib.util.module_from_spec(_spec)
assert _spec is not None and _spec.loader is not None
_spec.loader.exec_module(guardian)


class FakePool:
    def __init__(self, guardian_mode="CUSTOM", recent_rows=None, pending_count=1):
        self.guardian_mode = guardian_mode
        self.recent_rows = list(recent_rows or [])
        self.pending_count = pending_count
        self.executed = []

    async def fetchrow(self, query, *args):
        if "SELECT guardian_mode FROM users" in query:
            return {"guardian_mode": self.guardian_mode}
        return None

    async def fetch(self, query, *args):
        if "FROM guardian_queue" in query and "created_at > NOW() - make_interval" in query:
            return self.recent_rows
        return []

    async def fetchval(self, query, *args):
        if "SELECT COUNT(*) FROM guardian_queue WHERE uid = $1 AND consumed_at IS NULL" in query:
            return self.pending_count
        return 0

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return "OK"


def _load_insert_args(pool):
    for query, args in pool.executed:
        if "INSERT INTO guardian_queue" in query:
            return args
    raise AssertionError("guardian_queue insert not found")


def test_wake_word_enqueue_rewrites_message(monkeypatch):
    pool = FakePool()
    logged = []

    async def fake_get_pool():
        return pool

    async def fake_log_pipeline_event(**kwargs):
        logged.append(kwargs)

    monkeypatch.setattr(guardian, "_get_pool", fake_get_pool)
    monkeypatch.setattr(guardian, "_log_pipeline_event", fake_log_pipeline_event)
    monkeypatch.setattr(guardian, "_verify_key", lambda *args, **kwargs: None)

    req = guardian.EnqueueRequest(
        uid="uid-1",
        url="",
        priority="normal",
        message="I heard you. I am checking that now: hey ella what time is it",
        trigger="wake_word",
        metadata={"trace_id": "trace-1"},
    )

    result = asyncio.run(guardian.enqueue(req))
    insert_args = _load_insert_args(pool)

    assert result["ok"] is True
    assert insert_args[4] == guardian.WAKE_WORD_ACK_TEXT
    assert json.loads(insert_args[6])["trace_id"] == "trace-1"
    assert any(event["stage"] == "queue_inserted" for event in logged)


def test_enqueue_skips_duplicate_message_for_same_trace(monkeypatch):
    pool = FakePool(
        recent_rows=[
            {
                "id": "guardian_existing",
                "priority": "normal",
                "message": guardian.WAKE_WORD_ACK_TEXT,
                "trigger_type": "wake_word",
                "metadata": {"trace_id": "trace-dup"},
                "consumed_at": None,
                "created_at": None,
            }
        ]
    )
    logged = []

    async def fake_get_pool():
        return pool

    async def fake_log_pipeline_event(**kwargs):
        logged.append(kwargs)

    monkeypatch.setattr(guardian, "_get_pool", fake_get_pool)
    monkeypatch.setattr(guardian, "_log_pipeline_event", fake_log_pipeline_event)
    monkeypatch.setattr(guardian, "_verify_key", lambda *args, **kwargs: None)

    req = guardian.EnqueueRequest(
        uid="uid-1",
        url="",
        priority="normal",
        message="something quoted from transcript",
        trigger="wake_word",
        metadata={"trace_id": "trace-dup"},
    )

    result = asyncio.run(guardian.enqueue(req))

    assert result["skipped"] is True
    assert result["reason"] == "duplicate_message_same_trace"
    assert not any("INSERT INTO guardian_queue" in query for query, _ in pool.executed)
    assert logged[-1]["stage"] == "queue_rejected"


def test_enqueue_skips_wake_word_when_actionable_audio_already_pending(monkeypatch):
    pool = FakePool(
        recent_rows=[
            {
                "id": "guardian_urgent",
                "priority": "urgent",
                "message": "I need you to respond right now.",
                "trigger_type": "policy_dispatch",
                "metadata": {"trace_id": "trace-pending"},
                "consumed_at": None,
                "created_at": None,
            }
        ]
    )
    logged = []

    async def fake_get_pool():
        return pool

    async def fake_log_pipeline_event(**kwargs):
        logged.append(kwargs)

    monkeypatch.setattr(guardian, "_get_pool", fake_get_pool)
    monkeypatch.setattr(guardian, "_log_pipeline_event", fake_log_pipeline_event)
    monkeypatch.setattr(guardian, "_verify_key", lambda *args, **kwargs: None)

    req = guardian.EnqueueRequest(
        uid="uid-1",
        url="",
        priority="normal",
        message="some wake text",
        trigger="wake_word",
        metadata={"trace_id": "trace-pending"},
    )

    result = asyncio.run(guardian.enqueue(req))

    assert result["skipped"] is True
    assert result["reason"] == "wake_word_suppressed_same_trace_has_actionable_audio"
    assert not any("INSERT INTO guardian_queue" in query for query, _ in pool.executed)
    assert logged[-1]["metadata"]["existing_trigger_type"] == "policy_dispatch"


def test_urgent_enqueue_supersedes_pending_wake_word(monkeypatch):
    pool = FakePool(
        recent_rows=[
            {
                "id": "guardian_old_wake",
                "priority": "normal",
                "message": guardian.WAKE_WORD_ACK_TEXT,
                "trigger_type": "wake_word",
                "metadata": {"trace_id": "trace-urgent"},
                "consumed_at": None,
                "created_at": None,
            }
        ],
        pending_count=1,
    )
    logged = []

    async def fake_get_pool():
        return pool

    async def fake_log_pipeline_event(**kwargs):
        logged.append(kwargs)

    monkeypatch.setattr(guardian, "_get_pool", fake_get_pool)
    monkeypatch.setattr(guardian, "_log_pipeline_event", fake_log_pipeline_event)
    monkeypatch.setattr(guardian, "_verify_key", lambda *args, **kwargs: None)

    req = guardian.EnqueueRequest(
        uid="uid-1",
        url="https://example.test/audio.mp3",
        priority="urgent",
        message="This is the real urgent alert.",
        trigger="policy_dispatch",
        metadata={"trace_id": "trace-urgent"},
    )

    result = asyncio.run(guardian.enqueue(req))
    insert_args = _load_insert_args(pool)
    insert_metadata = json.loads(insert_args[6])

    assert result["ok"] is True
    assert any(
        "UPDATE guardian_queue SET consumed_at = NOW()" in query and args[0] == ["guardian_old_wake"]
        for query, args in pool.executed
    )
    assert insert_metadata["superseded_wake_word_ids"] == ["guardian_old_wake"]
    assert any(event["stage"] == "queue_inserted" for event in logged)
