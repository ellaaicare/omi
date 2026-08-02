"""Production-route authentication tests for Ella chat and voice."""

import sys
import types

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.modules.setdefault("websockets", types.SimpleNamespace())
sys.modules.setdefault("asyncpg", types.SimpleNamespace(Pool=object, create_pool=None))
conversations_module = types.ModuleType("database.conversations")
conversations_module._decrypt_conversation_data = lambda conversation, _uid: conversation
sys.modules.setdefault("database.conversations", conversations_module)

from ella.routers import chat, voice
from utils.ella import exact_firebase_auth


class _VoicePool:
    def __init__(self):
        self.fetchrow_calls = []

    async def fetchrow(self, query, *args):
        self.fetchrow_calls.append((query, args))
        return None


def _client(monkeypatch):
    def verify_token(token):
        if token == "valid-a":
            return {"uid": "uid-a"}
        raise ValueError("expired or invalid")

    monkeypatch.setattr(exact_firebase_auth.firebase_auth, "verify_id_token", verify_token)
    app = FastAPI()
    app.include_router(chat.router)
    app.include_router(voice.router)
    return TestClient(app)


def test_chat_owner_token_supplies_subject_without_caller_uid(monkeypatch):
    seen_uids = []

    async def stream(_message, uid, *_args, **_kwargs):
        seen_uids.append(uid)
        yield "done: e30=\n\n"

    monkeypatch.setattr(chat, "_stream_hermes_chat", stream)
    monkeypatch.setattr(chat, "CHAT_PLATFORM", "hermes")
    client = _client(monkeypatch)

    response = client.post(
        "/v1/ella/chat/stream",
        headers={"Authorization": "Bearer valid-a"},
        json={"message": "hello"},
    )

    assert response.status_code == 200
    assert seen_uids == ["uid-a"]


def test_voice_owner_token_supplies_subject_without_caller_uid(monkeypatch):
    pool = _VoicePool()
    monkeypatch.setattr(voice, "_pool", pool)
    monkeypatch.setattr(voice, "create_session_token", lambda **kwargs: f"token-for-{kwargs['uid']}")
    monkeypatch.setitem(voice.V2V_PROVIDERS["grok-voice"], "key_check", lambda: True)
    client = _client(monkeypatch)

    response = client.post(
        "/v1/voice/session",
        headers={"Authorization": "Bearer valid-a"},
        json={"provider": "grok-voice"},
    )

    assert response.status_code == 200
    assert response.json()["session_token"] == "token-for-uid-a"
    assert pool.fetchrow_calls[0][1] == ("uid-a",)


def test_user_routes_reject_uid_mismatch_before_provider_or_context_work(monkeypatch):
    pool = _VoicePool()
    chat_calls = []

    async def stream(*_args, **_kwargs):
        chat_calls.append(True)
        yield "done: e30=\n\n"

    monkeypatch.setattr(chat, "_stream_hermes_chat", stream)
    monkeypatch.setattr(voice, "_pool", pool)
    client = _client(monkeypatch)

    chat_response = client.post(
        "/v1/ella/chat/stream",
        headers={"Authorization": "Bearer valid-a"},
        json={"uid": "uid-b", "message": "hello"},
    )
    history_response = client.get(
        "/v1/ella/chat/history?uid=uid-b",
        headers={"Authorization": "Bearer valid-a"},
    )
    voice_body_response = client.post(
        "/v1/voice/session",
        headers={"Authorization": "Bearer valid-a"},
        json={"uid": "uid-b", "provider": "grok-voice"},
    )
    voice_query_response = client.post(
        "/v1/voice/session?uid=uid-b",
        headers={"Authorization": "Bearer valid-a"},
        json={"provider": "grok-voice"},
    )

    assert [
        chat_response.status_code,
        history_response.status_code,
        voice_body_response.status_code,
        voice_query_response.status_code,
    ] == [403, 403, 403, 403]
    assert chat_calls == []
    assert pool.fetchrow_calls == []


def test_user_routes_reject_missing_or_expired_token_before_work(monkeypatch):
    pool = _VoicePool()
    monkeypatch.setattr(voice, "_pool", pool)
    client = _client(monkeypatch)

    for headers in ({}, {"Authorization": "Bearer expired"}):
        assert client.post("/v1/ella/chat/stream", headers=headers, json={"message": "hello"}).status_code == 401
        assert client.get("/v1/ella/chat/history", headers=headers).status_code == 401
        assert client.post("/v1/voice/session", headers=headers, json={"provider": "grok-voice"}).status_code == 401
        assert client.post("/v1/voice/tts", headers=headers, json={"text": "hello"}).status_code == 401

    assert pool.fetchrow_calls == []


def test_voice_tts_allows_only_explicit_internal_guardian_service_key(monkeypatch):
    monkeypatch.setattr(voice, "ELLA_INTERNAL_TTS_KEY", "internal-key")
    client = _client(monkeypatch)

    response = client.post(
        "/v1/voice/tts",
        headers={"X-Guardian-Key": "internal-key", "X-TTS-Provider": "grok-voice"},
        json={"text": "hello"},
    )
    denied = client.post(
        "/v1/voice/tts",
        headers={"X-Guardian-Key": "wrong", "X-TTS-Provider": "grok-voice"},
        json={"text": "hello"},
    )

    assert response.status_code == 422
    assert denied.status_code == 401
