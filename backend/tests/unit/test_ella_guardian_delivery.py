import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest
from fastapi import HTTPException


sys.modules.setdefault("asyncpg", types.SimpleNamespace(Pool=object, create_pool=None))
sys.modules.setdefault("python_multipart", types.SimpleNamespace(__version__="0.0.20"))

_BACKEND = Path(__file__).resolve().parents[2]
_POLICY_PATH = _BACKEND / "ella" / "services" / "escalation_policy.py"
_POLICY_SPEC = importlib.util.spec_from_file_location("ella.services.escalation_policy", _POLICY_PATH)
policy = importlib.util.module_from_spec(_POLICY_SPEC)
assert _POLICY_SPEC and _POLICY_SPEC.loader
sys.modules.setdefault("ella", types.ModuleType("ella"))
sys.modules.setdefault("ella.services", types.ModuleType("ella.services"))
sys.modules["ella.services.escalation_policy"] = policy
_POLICY_SPEC.loader.exec_module(policy)

sys.modules.setdefault("ella.routers", types.ModuleType("ella.routers"))
resolve_module = types.ModuleType("ella.routers.resolve")
resolve_module.resolve_user_routing = None
sys.modules["ella.routers.resolve"] = resolve_module

_ROUTER_PATH = _BACKEND / "ella" / "routers" / "guardian.py"
_ROUTER_SPEC = importlib.util.spec_from_file_location("ella_guardian_under_test", _ROUTER_PATH)
guardian = importlib.util.module_from_spec(_ROUTER_SPEC)
assert _ROUTER_SPEC and _ROUTER_SPEC.loader
_ROUTER_SPEC.loader.exec_module(guardian)


class _FakePool:
    def __init__(self, user_row=None, caregiver_rows=None, existing_rows=None):
        self.user_row = user_row
        self.caregiver_rows = caregiver_rows or []
        self.existing_rows = existing_rows or []
        self.executed = []

    async def fetchrow(self, *_args):
        return self.user_row

    async def fetch(self, query, *_args):
        if "FROM caregivers" in query:
            return self.caregiver_rows
        if "FROM guardian_delivery_log" in query:
            return self.existing_rows
        return []

    async def fetchval(self, *_args):
        return None

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return "OK"


class _FakeResponse:
    status_code = 200
    text = "ok"


class _FakeAsyncClient:
    posts = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return _FakeResponse()


def _user_row(identities=None, phone_number="+15550000001"):
    return {
        "id": "user-1",
        "omi_uid": "uid-1",
        "guardian_mode": "off",
        "email": "user@example.test",
        "phone_number": phone_number,
        "identities": identities or {},
    }


def _caregiver_row():
    return {
        "id": "caregiver-1",
        "status": "ACTIVE",
        "is_emergency_contact": True,
        "name": "Care Giver",
        "relationship": "daughter",
        "email": "caregiver@example.test",
        "phone": "+15550000002",
        "permissions": {},
    }


def _decision():
    step = {
        "target": "user",
        "channel": "imessage",
        "priority": "urgent",
        "reason": "selected",
        "reason_code": "selected",
    }
    return types.SimpleNamespace(
        decision="notify_now",
        delivery_plan=(step,),
        to_dict=lambda: {
            "decision": "notify_now",
            "reason": "selected",
            "trace_id": "trace-1",
            "requires_ack": True,
            "delivery_plan": [dict(step)],
            "selected_channels": [dict(step)],
            "suppressed_channels": [],
            "policy_snapshot": {},
        },
    )


def test_load_delivery_context_uses_phone_number_fallback(monkeypatch):
    monkeypatch.setattr(guardian, "_pool", _FakePool(user_row=_user_row(identities={})))

    user, _caregivers = asyncio.run(guardian._load_delivery_context("uid-1"))

    assert user.user_phone == "+15550000001"


def test_load_delivery_context_prefers_identities_phone(monkeypatch):
    monkeypatch.setattr(
        guardian,
        "_pool",
        _FakePool(user_row=_user_row(identities={"phone": "+15550000099"})),
    )

    user, _caregivers = asyncio.run(guardian._load_delivery_context("uid-1"))

    assert user.user_phone == "+15550000099"


def test_reserve_delivery_steps_treats_success_as_already_sent(monkeypatch):
    pool = _FakePool(existing_rows=[{"channel": "imessage", "target": "user", "status": "success"}])
    monkeypatch.setattr(guardian, "_pool", pool)

    pending, skipped = asyncio.run(
        guardian._reserve_delivery_steps(
            "trace-1",
            "uid-1",
            [{"channel": "imessage", "target": "user", "recipient_phone": "+15550000001"}],
        )
    )

    assert pending == []
    assert skipped[0]["skip_reason"] == "already_success"
    assert pool.executed == []


def test_deliver_dispatches_pending_backend_resolved_recipient(monkeypatch):
    pool = _FakePool(user_row=_user_row(), caregiver_rows=[_caregiver_row()])
    _FakeAsyncClient.posts = []
    monkeypatch.setattr(guardian, "_pool", pool)
    monkeypatch.setattr(guardian, "evaluate_escalation_policy", lambda *_args: _decision())
    monkeypatch.setattr(guardian.httpx, "AsyncClient", _FakeAsyncClient)

    result = asyncio.run(
        guardian.deliver(
            guardian.DeliverRequest(uid="uid-1", trace_id="trace-1", severity="critical", summary="Needs help"),
            x_guardian_key=guardian.GUARDIAN_WEBHOOK_KEY,
        )
    )

    assert result["dispatched"] is True
    assert len(_FakeAsyncClient.posts) == 1
    _url, kwargs = _FakeAsyncClient.posts[0]
    assert kwargs["headers"]["X-Guardian-Key"] == guardian.GUARDIAN_WEBHOOK_KEY
    step = kwargs["json"]["delivery_plan"][0]
    assert step["target"] == "user"
    assert step["recipient_phone"] == "+15550000001"


def test_trace_log_allows_missing_key_but_rejects_bad_key(monkeypatch):
    pool = _FakePool()
    monkeypatch.setattr(guardian, "_pool", pool)

    ok = asyncio.run(
        guardian.log_pipeline_event(
            guardian.TraceLogRequest(trace_id="trace-1", uid="uid-1", stage="scanner_classified"),
            x_guardian_key=None,
            key=None,
        )
    )
    assert ok["logged"] is True

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            guardian.log_pipeline_event(
                guardian.TraceLogRequest(trace_id="trace-1", uid="uid-1", stage="scanner_classified"),
                x_guardian_key="bad",
            )
        )
    assert exc.value.status_code == 403
