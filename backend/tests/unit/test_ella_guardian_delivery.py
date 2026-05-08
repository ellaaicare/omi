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

app_settings_module = types.ModuleType("database.app_settings")
app_settings_module.get_voice_settings = lambda _uid: {}
app_settings_module.save_voice_settings = lambda _uid, voice: voice
sys.modules["database.app_settings"] = app_settings_module

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
    content = b"mp3-bytes"
    headers = {"content-type": "audio/mpeg"}


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


def test_synthesize_audio_resolves_server_voice_settings(monkeypatch):
    _FakeAsyncClient.posts = []
    monkeypatch.setattr(guardian.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(guardian.app_settings_db, "get_voice_settings", lambda uid: {"voice_mode": "grok-voice"})

    response = asyncio.run(
        guardian.synthesize_audio(
            guardian.SynthesizeRequest(uid="uid-1", text="Hello", trace_id="trace-1"),
            x_guardian_key=guardian.GUARDIAN_WEBHOOK_KEY,
        )
    )

    assert response.body == b"mp3-bytes"
    url, kwargs = _FakeAsyncClient.posts[0]
    assert url == guardian.ELLA_INTERNAL_VOICE_TTS_URL
    assert kwargs["headers"]["X-TTS-Provider"] == "kokoro"
    assert kwargs["json"]["text"] == "Hello"
    assert response.headers["x-guardian-tts-provider"] == "kokoro"
    assert response.headers["x-guardian-voice-mode"] == "grok-voice"
    assert response.headers["x-guardian-tts-candidates"] == "kokoro,elevenlabs"


def test_synthesize_audio_uses_matching_tts_provider(monkeypatch):
    _FakeAsyncClient.posts = []
    monkeypatch.setattr(guardian.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(guardian.app_settings_db, "get_voice_settings", lambda uid: {"voice_mode": "fish-audio-s2"})

    response = asyncio.run(
        guardian.synthesize_audio(
            guardian.SynthesizeRequest(uid="uid-1", text="Hello", trace_id="trace-1"),
            x_guardian_key=guardian.GUARDIAN_WEBHOOK_KEY,
        )
    )

    assert response.body == b"mp3-bytes"
    _url, kwargs = _FakeAsyncClient.posts[0]
    assert kwargs["headers"]["X-TTS-Provider"] == "fish-audio-s2"
    assert response.headers["x-guardian-tts-provider"] == "fish-audio-s2"
    assert response.headers["x-guardian-tts-candidates"] == "fish-audio-s2,fish-audio,kokoro,elevenlabs"


def test_synthesize_audio_falls_back_to_next_candidate(monkeypatch):
    class _FallbackResponse:
        text = "ok"
        headers = {"content-type": "audio/mpeg"}

        def __init__(self, status_code, content=b"mp3-bytes"):
            self.status_code = status_code
            self.content = content

    class _FallbackAsyncClient:
        posts = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, **kwargs):
            self.posts.append((url, kwargs))
            provider = kwargs["headers"]["X-TTS-Provider"]
            if provider == "fish-audio-s2":
                return _FallbackResponse(502, b"")
            return _FallbackResponse(200, b"fallback-mp3")

    _FallbackAsyncClient.posts = []
    monkeypatch.setattr(guardian.httpx, "AsyncClient", _FallbackAsyncClient)
    monkeypatch.setattr(guardian.app_settings_db, "get_voice_settings", lambda uid: {"voice_mode": "fish-audio-s2"})

    response = asyncio.run(
        guardian.synthesize_audio(
            guardian.SynthesizeRequest(uid="uid-1", text="Hello", trace_id="trace-1"),
            x_guardian_key=guardian.GUARDIAN_WEBHOOK_KEY,
        )
    )

    assert response.body == b"fallback-mp3"
    attempted = [kwargs["headers"]["X-TTS-Provider"] for _url, kwargs in _FallbackAsyncClient.posts]
    assert attempted == ["fish-audio-s2", "fish-audio"]
    assert response.headers["x-guardian-tts-provider"] == "fish-audio"
    assert response.headers["x-guardian-fallback-used"] == "true"


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
