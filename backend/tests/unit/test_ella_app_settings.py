import importlib
import sys
import types

from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.cloud.firestore_v1 import SERVER_TIMESTAMP

from ella.services import app_settings as service
from utils.other import endpoints as auth


def test_extract_voice_settings_normalizes_ios_payload():
    voice = service.extract_voice_settings(
        {
            "voice_mode": "gemini-live",
            "tts_provider": "gemini-live",
            "conversation_provider": "gemini-live",
            "settings": {
                "voice": {
                    "uses_v2v_session": True,
                    "source_client": "ios-app",
                    "source_setting": "devTtsProvider",
                    "client_version": "1.0.524+780",
                    "updated_at": "2026-05-08T18:00:00Z",
                }
            },
        }
    )

    assert voice["voice_mode"] == "gemini-native-live"
    assert voice["tts_provider"] == "gemini-native-live"
    assert voice["conversation_provider"] == "gemini-native-live"
    assert voice["uses_v2v_session"] is True
    assert voice["session_voice_mode"] == "gemini-native-live-v1"
    assert voice["source_client"] == "ios-app"


def test_effective_settings_maps_v2v_to_explicit_one_shot_fallback():
    effective = service.build_effective_voice_settings("uid-1", {"voice_mode": "grok-voice"})

    assert effective["voice_mode"] == "grok-voice"
    assert effective["one_shot_tts_provider"] == "xai-tts"
    assert effective["effective_voice_settings"]["one_shot_tts_candidates"] == ["xai-tts", "kokoro", "elevenlabs"]
    assert effective["effective_voice_settings"]["provider_type"] == "v2v"
    assert effective["effective_voice_settings"]["fallback_used"] is True
    assert "Guardian one-shots" in effective["effective_voice_settings"]["fallback_reason"]


def test_effective_settings_maps_tts_mode_to_closest_guardian_candidates():
    effective = service.build_effective_voice_settings("uid-1", {"voice_mode": "fish-audio-s2"})

    assert effective["one_shot_tts_provider"] == "fish-audio-s2"
    assert effective["effective_voice_settings"]["one_shot_tts_candidates"] == [
        "fish-audio-s2",
        "fish-audio",
        "kokoro",
        "elevenlabs",
    ]
    assert effective["effective_voice_settings"]["fallback_used"] is False


def _load_router(monkeypatch, stored_voice=None):
    fake_db = types.ModuleType("database.app_settings")
    state = {"voice": stored_voice or {}}

    def get_voice_settings(uid):
        assert uid == "uid-1"
        return dict(state["voice"])

    def save_voice_settings(uid, voice):
        assert uid == "uid-1"
        state["voice"] = {**voice, "server_updated_at": SERVER_TIMESTAMP}
        return dict(state["voice"])

    fake_db.get_voice_settings = get_voice_settings
    fake_db.save_voice_settings = save_voice_settings
    monkeypatch.setitem(sys.modules, "database.app_settings", fake_db)
    sys.modules.pop("ella.routers.settings", None)
    if not hasattr(sys.modules.get("ella.routers"), "__path__"):
        sys.modules.pop("ella.routers", None)
    module = importlib.import_module("ella.routers.settings")

    app = FastAPI()
    app.include_router(module.router)
    app.dependency_overrides[auth.get_current_user_uid] = lambda: "uid-1"
    return TestClient(app), state


def test_settings_router_accepts_ios_patch_and_returns_effective(monkeypatch):
    client, state = _load_router(monkeypatch)

    response = client.patch(
        "/v1/ella/settings",
        json={
            "voice_mode": "openai-realtime",
            "settings": {"voice": {"source_client": "ios-app", "source_setting": "devTtsProvider"}},
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["voice_mode"] == "openai-native-realtime"
    assert state["voice"]["session_voice_mode"] == "openai-native-realtime-v1"
    assert payload["settings"]["voice"]["server_updated_at"] is None

    effective = client.get("/v1/ella/settings/effective").json()
    assert effective["voice_mode"] == "openai-native-realtime"
    assert effective["effective_voice_settings"]["one_shot_tts_provider"] == "kokoro"


def test_settings_router_returns_default_when_no_server_setting(monkeypatch):
    client, _state = _load_router(monkeypatch)

    response = client.get("/v1/ella/settings")

    assert response.status_code == 200
    assert response.json()["voice_mode"] == "elevenlabs"
