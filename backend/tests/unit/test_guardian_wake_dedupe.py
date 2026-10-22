import asyncio
import importlib.util
import sys
import types
from datetime import datetime, timezone
from pathlib import Path


def load_guardian_module():
    python_multipart_stub = types.ModuleType("python_multipart")
    python_multipart_stub.__version__ = "99.0.0"
    sys.modules["python_multipart"] = python_multipart_stub
    multipart_stub = types.ModuleType("multipart")
    multipart_stub.__version__ = "99.0.0"
    multipart_inner_stub = types.ModuleType("multipart.multipart")
    multipart_inner_stub.parse_options_header = lambda *_args, **_kwargs: {}
    sys.modules["multipart"] = multipart_stub
    sys.modules["multipart.multipart"] = multipart_inner_stub

    sys.modules.setdefault("ella", types.ModuleType("ella"))
    sys.modules.setdefault("ella.routers", types.ModuleType("ella.routers"))

    resolve_stub = types.ModuleType("ella.routers.resolve")

    async def resolve_user_routing(_uid):
        return None

    resolve_stub.resolve_user_routing = resolve_user_routing
    sys.modules["ella.routers.resolve"] = resolve_stub

    policy_stub = types.ModuleType("ella.services.escalation_policy")
    policy_stub.MODE_EMERGENCY_ONLY = "EMERGENCY_ONLY"
    policy_stub.MODE_OFF = "OFF"
    policy_stub.CaregiverPolicyContext = object
    policy_stub.EscalationEvent = object
    policy_stub.UserPolicyContext = object
    policy_stub.evaluate_escalation_policy = lambda *_args, **_kwargs: None
    policy_stub._normalize_mode = lambda mode: "OFF" if mode is None else str(mode).upper()
    sys.modules.setdefault("ella.services", types.ModuleType("ella.services"))
    sys.modules["ella.services.escalation_policy"] = policy_stub

    module_path = Path(__file__).parents[2] / "ella" / "routers" / "guardian.py"
    spec = importlib.util.spec_from_file_location("guardian_under_test", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakePool:
    def __init__(self, existing_rows):
        self.existing_rows = existing_rows
        self.executed = []

    async def fetchrow(self, *_args):
        return {"guardian_mode": "ACTIVE_SUPPORT"}

    async def fetch(self, *_args):
        return self.existing_rows

    async def fetchval(self, *_args):
        return 1

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return "OK"


def test_fastpath_duplicate_after_hermes_trace_is_rejected():
    guardian = load_guardian_module()
    trace_id = "fb1cf228-0af1-43bd-b235-7cc9ad868a4a"
    pool = FakePool(
        [
            {
                "id": "guardian_338b2d3c2251",
                "trigger_type": "wake_word_user_support",
                "consumed_at": datetime.now(timezone.utc),
                "metadata": {
                    "trace_id": trace_id,
                    "source": "scanner_wake_word_user_support",
                    "hermes_response_present": True,
                },
            }
        ]
    )

    async def fake_get_pool():
        return pool

    async def fake_log_pipeline_event(**_kwargs):
        return None

    guardian._get_pool = fake_get_pool
    guardian._log_pipeline_event = fake_log_pipeline_event

    req = guardian.EnqueueRequest(
        userID="test-uid",
        url="",
        message="I heard you. I am checking that now.",
        trigger="wake_word",
        metadata={"trace_id": trace_id, "source": "scanner_fastpath_semantic_wake"},
    )

    result = asyncio.run(guardian.enqueue(req, x_guardian_key=guardian.GUARDIAN_WEBHOOK_KEY))

    assert result["rejected"] is True
    assert result["reason"] == "duplicate_wake_word_trace"
    assert result["existing_id"] == "guardian_338b2d3c2251"
    assert not any("INSERT INTO guardian_queue" in query for query, _args in pool.executed)


def test_hermes_response_supersedes_pending_fastpath_same_trace():
    guardian = load_guardian_module()
    trace_id = "trace-hermes-wins"
    pool = FakePool(
        [
            {
                "id": "guardian_fastpath",
                "trigger_type": "wake_word",
                "consumed_at": None,
                "metadata": {"trace_id": trace_id, "source": "scanner_fastpath_semantic_wake"},
            }
        ]
    )

    async def fake_get_pool():
        return pool

    async def fake_log_pipeline_event(**_kwargs):
        return None

    guardian._get_pool = fake_get_pool
    guardian._log_pipeline_event = fake_log_pipeline_event

    req = guardian.EnqueueRequest(
        id="guardian_hermes",
        userID="test-uid",
        url="",
        message="Full Hermes response",
        trigger="wake_word_user_support",
        metadata={
            "trace_id": trace_id,
            "source": "scanner_wake_word_user_support",
            "hermes_response_present": True,
        },
    )

    result = asyncio.run(guardian.enqueue(req, x_guardian_key=guardian.GUARDIAN_WEBHOOK_KEY))

    assert result["ok"] is True
    assert result["id"] == "guardian_hermes"
    assert any("UPDATE guardian_queue" in query for query, _args in pool.executed)
    assert any("INSERT INTO guardian_queue" in query for query, _args in pool.executed)
