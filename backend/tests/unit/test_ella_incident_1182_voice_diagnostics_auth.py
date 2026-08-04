import sys
from types import ModuleType
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.modules.setdefault("database._client", MagicMock())
sys.modules.setdefault("websockets", ModuleType("websockets"))
conversations_module = ModuleType("database.conversations")
conversations_module._decrypt_conversation_data = lambda value: value
sys.modules.setdefault("database.conversations", conversations_module)

from ella.routers import voice
from utils.ella import exact_firebase_auth


def test_voice_diagnostics_require_exact_firebase_bearer(monkeypatch):
    def verify(token):
        if token == "token-a":
            return {"uid": "uid-a"}
        raise ValueError("invalid token")

    monkeypatch.setattr(exact_firebase_auth.firebase_auth, "verify_id_token", verify)
    app = FastAPI()
    app.include_router(voice.router)
    client = TestClient(app)

    for path in ("/v1/voice/health", "/v1/voice/providers"):
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"Authorization": "Bearer invalid"}).status_code == 401
        assert client.get(path, headers={"Authorization": "Bearer token-a"}).status_code == 200

    health = client.get(
        "/v1/voice/health",
        headers={"Authorization": "Bearer token-a"},
    )
    assert health.json() == {"status": "ok", "service": "ella-voice"}
    serialized = health.text.lower()
    assert "url" not in serialized
    assert "secret" not in serialized
    assert "configured" not in serialized
