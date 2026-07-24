import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

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

ella_app_settings_module = types.ModuleType("ella.services.app_settings")
ella_app_settings_module.TTS_PROVIDERS = {"kokoro", "elevenlabs", "fish-audio", "fish-audio-s2", "xai-tts"}
ella_app_settings_module.build_effective_voice_settings = lambda _uid, voice: {
    "effective_voice_settings": {
        "voice_mode": voice.get("voice_mode", "elevenlabs") if isinstance(voice, dict) else "elevenlabs",
        "one_shot_tts_provider": {
            "fish-audio-s2": "fish-audio-s2",
            "grok-voice": "xai-tts",
        }.get(voice.get("voice_mode") if isinstance(voice, dict) else None, "kokoro"),
        "one_shot_tts_candidates": {
            "fish-audio-s2": ["fish-audio-s2", "fish-audio", "kokoro", "elevenlabs"],
            "grok-voice": ["xai-tts", "kokoro", "elevenlabs"],
        }.get(voice.get("voice_mode") if isinstance(voice, dict) else None, ["kokoro", "elevenlabs"]),
    }
}
sys.modules["ella.services.app_settings"] = ella_app_settings_module

runtime_resolver_module = types.ModuleType("ella.services.runtime_resolver")
runtime_resolver_module.runtime_bindings_enabled = lambda _uid: False
sys.modules["ella.services.runtime_resolver"] = runtime_resolver_module

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


class _FakeAlertPool:
    def __init__(self, rows, timezone_name="America/Los_Angeles"):
        self.rows = rows
        self.timezone_name = timezone_name
        self.fetch_args = []
        self.fetchrow_args = []

    async def fetchrow(self, _query, *args):
        self.fetchrow_args.append(args)
        return {"timezone": self.timezone_name}

    async def fetch(self, query, *args):
        self.fetch_args.append(args)
        self.fetch_query = query
        return self.rows


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
    assert kwargs["headers"]["X-TTS-Provider"] == "xai-tts"
    assert kwargs["json"]["text"] == "Hello"
    assert response.headers["x-guardian-tts-provider"] == "xai-tts"
    assert response.headers["x-guardian-voice-mode"] == "grok-voice"
    assert response.headers["x-guardian-tts-candidates"] == "xai-tts,kokoro,elevenlabs"


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


def test_enqueue_rejects_guardian_wake_fallback_echo_message():
    rejected, reason = guardian._enqueue_rejects_guardian_echo(
        "uid-1",
        guardian.EnqueueRequest(
            uid="uid-1",
            url="https://example.test/audio.mp3",
            trigger="wake_word_fallback",
            message="I heard you. I am checking that now: Hi, Greg. I heard my name. I'm here with you.",
        ),
    )

    assert rejected is True
    assert reason == "guardian_playback_echo"


def test_enqueue_does_not_reject_real_wake_word_question():
    rejected, reason = guardian._enqueue_rejects_guardian_echo(
        "uid-1",
        guardian.EnqueueRequest(
            uid="uid-1",
            url="https://example.test/audio.mp3",
            trigger="wake_word_fallback",
            message="I heard you. I am checking that now: Hey Ella, where did I put my glasses?",
        ),
    )

    assert rejected is False
    assert reason is None


def test_wake_ack_request_detects_trigger_and_metadata_flag():
    assert guardian._is_wake_ack_request(
        guardian.EnqueueRequest(
            uid="uid-1",
            url="https://example.test/wake.mp3",
            trigger="wake_word_ack",
        )
    )
    assert guardian._is_wake_ack_request(
        guardian.EnqueueRequest(
            uid="uid-1",
            url="https://example.test/wake.mp3",
            metadata={"ack_only": True},
        )
    )
    assert not guardian._is_wake_ack_request(
        guardian.EnqueueRequest(
            uid="uid-1",
            url="https://example.test/full.mp3",
            trigger="wake_word_user_support",
        )
    )


def test_wake_word_row_matches_fallback_variants():
    assert guardian._is_wake_word_row({"trigger_type": "wake_word_fallback", "metadata": {}})


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


def test_guardian_alert_history_normalizes_queue_event_delivery_rows(monkeypatch):
    created = datetime(2026, 5, 15, 19, 38, tzinfo=timezone.utc)
    consumed = datetime(2026, 5, 15, 19, 39, tzinfo=timezone.utc)
    pool = _FakeAlertPool(
        [
            {
                "id": "guardian_abc",
                "uid": "uid-1",
                "url": "https://ella-ai-care.com/audio/uid-1/abc.mp3",
                "priority": "debug",
                "message": "  Ella found your glasses near the kitchen table.  ",
                "trigger_type": "wake_word_user_support",
                "metadata": {
                    "trace_id": "trace-1",
                    "queue_item_id": "guardian_abc",
                    "source_conversation_id": "omi-123",
                    "dry_run": True,
                },
                "created_at": created,
                "consumed_at": consumed,
                "trace_id": "trace-1",
                "events": [
                    {
                        "created_at": consumed.isoformat(),
                        "stage": "ios_playback_failed",
                        "status": "error",
                        "latency_ms": 50,
                        "metadata": {"queue_item_id": "guardian_abc", "error": "OSStatus -50"},
                    }
                ],
                "deliveries": [
                    {
                        "channel": "imessage",
                        "target": "caregiver",
                        "caregiver_id": "caregiver-1",
                        "status": "sent",
                    }
                ],
            }
        ]
    )
    monkeypatch.setattr(guardian, "_pool", pool)

    result = asyncio.run(guardian._guardian_alert_history("uid-1", 50))

    assert result["uid"] == "uid-1"
    assert result["count"] == 1
    alert = result["alerts"][0]
    assert alert["queue_item_id"] == "guardian_abc"
    assert alert["summary"] == "Ella found your glasses near the kitchen table."
    assert alert["trigger_type"] == "wake_word_user_support"
    assert alert["delivery_target"] == "caregiver"
    assert alert["playback_status"] == "failed"
    assert alert["source_conversation_id"] == "omi-123"
    assert alert["caregiver_escalation"] is True
    assert alert["escalation_status"] == "sent"
    assert alert["dry_run"] is True
    assert alert["test"] is True
    assert alert["created_time"]["timezone"] == "America/Los_Angeles"
    assert pool.fetch_args[0] == ("uid-1", 50)
    assert "COALESCE(trigger_type, '') <> 'wake_word_ack'" in pool.fetch_query
    assert "COALESCE(metadata->>'ack_only', '') <> 'true'" in pool.fetch_query


def test_guardian_alerts_endpoint_excludes_ack_only_rows_but_keeps_real_wake_words(monkeypatch):
    created = datetime(2026, 5, 15, 20, 0, tzinfo=timezone.utc)
    pool = _FakeAlertPool(
        [
            {
                "id": "guardian_ack_trigger",
                "uid": "auth-uid",
                "url": "https://example.test/wake-ack.mp3",
                "priority": "normal",
                "message": "wake_ack",
                "trigger_type": "wake_word_ack",
                "metadata": {"trace_id": "trace-ack-trigger"},
                "created_at": created,
                "consumed_at": None,
                "trace_id": "trace-ack-trigger",
                "events": [],
                "deliveries": [],
            },
            {
                "id": "guardian_ack_metadata",
                "uid": "auth-uid",
                "url": "https://example.test/wake-ack.mp3",
                "priority": "normal",
                "message": "Acknowledged",
                "trigger_type": "wake_word",
                "metadata": {"trace_id": "trace-ack-metadata", "ack_only": True},
                "created_at": created,
                "consumed_at": None,
                "trace_id": "trace-ack-metadata",
                "events": [],
                "deliveries": [],
            },
            {
                "id": "guardian_real_wake",
                "uid": "auth-uid",
                "url": "https://example.test/full-response.mp3",
                "priority": "normal",
                "message": "I found your glasses.",
                "trigger_type": "wake_word",
                "metadata": {"trace_id": "trace-real-wake", "ack_only": False},
                "created_at": created,
                "consumed_at": None,
                "trace_id": "trace-real-wake",
                "events": [],
                "deliveries": [],
            },
        ]
    )
    monkeypatch.setattr(guardian, "_pool", pool)

    app = FastAPI()
    app.include_router(guardian.alerts_router)
    app.dependency_overrides[guardian.auth.get_current_user_uid] = lambda: "auth-uid"

    response = TestClient(app).get("/v1/ella/guardian-alerts?limit=50")

    assert response.status_code == 200
    assert [alert["queue_item_id"] for alert in response.json()["alerts"]] == ["guardian_real_wake"]
    assert "COALESCE(trigger_type, '') <> 'wake_word_ack'" in pool.fetch_query
    assert "COALESCE(metadata->>'ack_only', '') <> 'true'" in pool.fetch_query


def test_guardian_alerts_endpoint_uses_authenticated_uid_not_query_uid(monkeypatch):
    pool = _FakeAlertPool(
        [
            {
                "id": "guardian_auth",
                "uid": "auth-uid",
                "url": "",
                "priority": "normal",
                "message": "Wake response",
                "trigger_type": "wake_word",
                "metadata": {"trace_id": "trace-auth"},
                "created_at": datetime(2026, 5, 15, 20, 0, tzinfo=timezone.utc),
                "consumed_at": None,
                "trace_id": "trace-auth",
                "events": [],
                "deliveries": [],
            }
        ]
    )
    monkeypatch.setattr(guardian, "_pool", pool)

    app = FastAPI()
    app.include_router(guardian.alerts_router)
    app.dependency_overrides[guardian.auth.get_current_user_uid] = lambda: "auth-uid"
    client = TestClient(app)

    response = client.get("/v1/ella/guardian-alerts?limit=50&uid=other-user")

    assert response.status_code == 200
    body = response.json()
    assert body["uid"] == "auth-uid"
    assert body["alerts"][0]["queue_item_id"] == "guardian_auth"
    assert pool.fetch_args[0] == ("auth-uid", 50)
