import asyncio
import importlib.util
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlparse

import jwt

sys.modules.setdefault("asyncpg", MagicMock())

_backend_path = Path(__file__).resolve().parents[2]
if str(_backend_path) not in sys.path:
    sys.path.insert(0, str(_backend_path))


def load_voice_module():
    os.environ["ELLA_SESSION_SECRET"] = "test-secret"
    os.environ["ELLA_VOICE_ENDPOINT"] = "wss://voice.ella-ai-care.com/ws"
    module_path = _backend_path / "ella" / "routers" / "voice.py"
    spec = importlib.util.spec_from_file_location("ella_voice_test_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_openclaw_direct_provider_is_registered():
    voice = load_voice_module()

    provider = voice.V2V_PROVIDERS["openclaw-direct"]

    assert provider["default_mode"] == "openclaw-direct-v1"
    assert provider["endpoint_env"] == "ELLA_VOICE_ENDPOINT"
    assert provider["key_check"]() is True


def test_openclaw_direct_session_token_claims():
    voice = load_voice_module()

    token = voice.create_session_token(
        uid="omi-user-1",
        firebase_uid="firebase-1",
        display_name="Margaret",
        provider="openclaw-direct",
        voice_mode="openclaw-direct-v1",
    )
    payload = jwt.decode(token, "test-secret", algorithms=["HS256"])

    assert payload["uid"] == "omi-user-1"
    assert payload["firebase_uid"] == "firebase-1"
    assert payload["name"] == "Margaret"
    assert payload["provider"] == "openclaw-direct"
    assert payload["voice_mode"] == "openclaw-direct-v1"
    assert payload["iss"] == "omi-backend"
    assert payload["context_url"].endswith("/v1/users/omi-user-1/context")
    assert payload["callback_url"].endswith("/v1/ella/voice-session")
    assert datetime.fromtimestamp(payload["exp"], tz=timezone.utc) > datetime.now(timezone.utc)


def test_openclaw_direct_endpoint_includes_mode_and_token():
    voice = load_voice_module()

    endpoint = voice.build_voice_endpoint(
        "wss://voice.ella-ai-care.com/ws",
        "openclaw-direct-v1",
        "jwt-token",
    )
    parsed = urlparse(endpoint)
    query = parse_qs(parsed.query)

    assert parsed.scheme == "wss"
    assert parsed.netloc == "voice.ella-ai-care.com"
    assert query["mode"] == ["openclaw-direct-v1"]
    assert query["token"] == ["jwt-token"]


def test_create_openclaw_direct_voice_session(monkeypatch):
    voice = load_voice_module()

    class FakePool:
        async def fetchrow(self, query, uid):
            return {"name": "Margaret"}

    async def fake_get_pool():
        return FakePool()

    monkeypatch.setattr(voice, "_get_pool", fake_get_pool)

    response = asyncio.run(
        voice.create_voice_session(
            body=voice.VoiceSessionRequest(
                uid="omi-user-1",
                provider="openclaw-direct",
            )
        )
    )

    assert response.provider == "openclaw-direct"
    assert response.voice_mode == "openclaw-direct-v1"
    assert response.audio_format["sample_rate"] == 24000
    assert "mode=openclaw-direct-v1" in response.voice_endpoint
    assert "token=" in response.voice_endpoint
