from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from google.cloud.firestore_v1.transforms import Sentinel


TTS_PROVIDERS = {"elevenlabs", "fish-audio", "fish-audio-s1", "fish-audio-s2", "kokoro", "inworld"}
V2V_PROVIDERS = {"openclaw-direct", "grok-voice", "gemini-native-live", "openai-native-realtime"}
VOICE_MODE_ALIASES = {
    "gemini-live": "gemini-native-live",
    "openai-realtime": "openai-native-realtime",
}
SUPPORTED_VOICE_MODES = TTS_PROVIDERS | V2V_PROVIDERS | set(VOICE_MODE_ALIASES)
NORMALIZED_SUPPORTED_VOICE_MODES = (TTS_PROVIDERS | V2V_PROVIDERS) - set(VOICE_MODE_ALIASES)

_DEFAULT_VOICE_MODE = os.getenv("ELLA_DEFAULT_VOICE_MODE", "elevenlabs")
_DEFAULT_ONE_SHOT_TTS_PROVIDER = os.getenv("ELLA_DEFAULT_GUARDIAN_TTS_PROVIDER", "kokoro")
_DEFAULT_ONE_SHOT_TTS_FALLBACK_CHAIN = os.getenv("ELLA_GUARDIAN_TTS_FALLBACK_CHAIN", "kokoro,elevenlabs")

_SESSION_MODE_BY_PROVIDER = {
    "openclaw-direct": "openclaw-direct-v1",
    "gemini-native-live": "gemini-native-live-v1",
    "openai-native-realtime": "openai-native-realtime-v1",
}

_ONE_SHOT_CANDIDATES_BY_VOICE_MODE = {
    "elevenlabs": ["elevenlabs", "kokoro"],
    "fish-audio": ["fish-audio", "fish-audio-s2", "kokoro", "elevenlabs"],
    "fish-audio-s1": ["fish-audio-s1", "fish-audio", "fish-audio-s2", "kokoro", "elevenlabs"],
    "fish-audio-s2": ["fish-audio-s2", "fish-audio", "kokoro", "elevenlabs"],
    "kokoro": ["kokoro", "fish-audio-s2", "elevenlabs"],
    "inworld": ["inworld", "kokoro", "elevenlabs"],
    # V2V modes keep conversation routing intact, but Guardian one-shots need
    # explicit TTS routing until provider-native one-shot adapters are wired in.
    "openclaw-direct": [os.getenv("ELLA_OPENCLAW_ONE_SHOT_TTS_PROVIDER", _DEFAULT_ONE_SHOT_TTS_PROVIDER)],
    "grok-voice": [os.getenv("ELLA_GROK_VOICE_ONE_SHOT_TTS_PROVIDER", _DEFAULT_ONE_SHOT_TTS_PROVIDER)],
    "gemini-native-live": [os.getenv("ELLA_GEMINI_LIVE_ONE_SHOT_TTS_PROVIDER", _DEFAULT_ONE_SHOT_TTS_PROVIDER)],
    "openai-native-realtime": [os.getenv("ELLA_OPENAI_REALTIME_ONE_SHOT_TTS_PROVIDER", _DEFAULT_ONE_SHOT_TTS_PROVIDER)],
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_voice_mode(value: Any) -> str:
    if not isinstance(value, str):
        return _normalized_default_voice_mode()
    candidate = value.strip().lower()
    candidate = VOICE_MODE_ALIASES.get(candidate, candidate)
    if candidate not in NORMALIZED_SUPPORTED_VOICE_MODES:
        return _normalized_default_voice_mode()
    return candidate


def _normalized_default_voice_mode() -> str:
    candidate = VOICE_MODE_ALIASES.get(str(_DEFAULT_VOICE_MODE).strip().lower(), str(_DEFAULT_VOICE_MODE).strip().lower())
    return candidate if candidate in NORMALIZED_SUPPORTED_VOICE_MODES else "elevenlabs"


def is_v2v_voice_mode(value: str) -> bool:
    return normalize_voice_mode(value) in V2V_PROVIDERS


def session_voice_mode(value: str) -> str | None:
    return _SESSION_MODE_BY_PROVIDER.get(normalize_voice_mode(value))


def one_shot_tts_provider(value: str) -> str:
    return one_shot_tts_candidates(value)[0]


def one_shot_tts_candidates(value: str) -> list[str]:
    """Return ordered Guardian one-shot providers, closest match first."""
    voice_mode = normalize_voice_mode(value)
    candidates = list(_ONE_SHOT_CANDIDATES_BY_VOICE_MODE.get(voice_mode, []))
    candidates.extend(_split_provider_chain(_DEFAULT_ONE_SHOT_TTS_FALLBACK_CHAIN))
    candidates.extend([_DEFAULT_ONE_SHOT_TTS_PROVIDER, "kokoro", "elevenlabs"])
    return _dedupe_supported_tts(candidates)


def _split_provider_chain(value: str) -> list[str]:
    return [item.strip().lower() for item in str(value or "").split(",") if item.strip()]


def _dedupe_supported_tts(candidates: list[str]) -> list[str]:
    result: list[str] = []
    for provider in candidates:
        normalized = str(provider or "").strip().lower()
        if normalized in TTS_PROVIDERS and normalized not in result:
            result.append(normalized)
    return result or ["kokoro"]


def extract_voice_settings(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract and normalize iOS voice settings from the flexible sync payload."""
    settings = payload.get("settings") if isinstance(payload.get("settings"), dict) else {}
    nested_voice = settings.get("voice") if isinstance(settings.get("voice"), dict) else {}

    raw_mode = (
        nested_voice.get("voice_mode")
        or payload.get("voice_mode")
        or nested_voice.get("tts_provider")
        or payload.get("tts_provider")
        or nested_voice.get("conversation_provider")
        or payload.get("conversation_provider")
        or _DEFAULT_VOICE_MODE
    )
    voice_mode = normalize_voice_mode(raw_mode)

    voice = {
        **nested_voice,
        "voice_mode": voice_mode,
        "tts_provider": voice_mode,
        "conversation_provider": voice_mode,
        "uses_v2v_session": is_v2v_voice_mode(voice_mode),
        "source_client": payload.get("source_client") or nested_voice.get("source_client") or "unknown",
        "source_setting": payload.get("source_setting") or nested_voice.get("source_setting") or "unknown",
        "client_version": payload.get("client_version") or nested_voice.get("client_version") or "unknown",
        "updated_at": payload.get("updated_at") or nested_voice.get("updated_at") or utc_now_iso(),
    }
    mode = session_voice_mode(voice_mode)
    if mode:
        voice["session_voice_mode"] = nested_voice.get("session_voice_mode") or mode
    else:
        voice.pop("session_voice_mode", None)
    return voice


def build_settings_response(uid: str, voice: dict[str, Any] | None = None) -> dict[str, Any]:
    resolved_voice = _json_safe(extract_voice_settings({"settings": {"voice": voice or {}}}))
    return _json_safe({
        "uid": uid,
        "voice_mode": resolved_voice["voice_mode"],
        "tts_provider": resolved_voice["tts_provider"],
        "conversation_provider": resolved_voice["conversation_provider"],
        "settings": {
            "voice": resolved_voice,
        },
        "updated_at": resolved_voice.get("updated_at"),
    })


def build_effective_voice_settings(uid: str, voice: dict[str, Any] | None = None) -> dict[str, Any]:
    resolved_voice = _json_safe(extract_voice_settings({"settings": {"voice": voice or {}}}))
    voice_mode = resolved_voice["voice_mode"]
    fallback_used = voice_mode in V2V_PROVIDERS
    fallback_reason = None
    if fallback_used:
        fallback_reason = f"{voice_mode} is a V2V mode; Guardian one-shots require a TTS provider"

    effective = {
        **resolved_voice,
        "one_shot_tts_provider": one_shot_tts_provider(voice_mode),
        "one_shot_tts_candidates": one_shot_tts_candidates(voice_mode),
        "provider_type": "v2v" if is_v2v_voice_mode(voice_mode) else "tts",
        "fallback_used": fallback_used,
        "fallback_reason": fallback_reason,
        "resolved_at": utc_now_iso(),
    }
    return _json_safe({
        "uid": uid,
        "voice_mode": voice_mode,
        "tts_provider": resolved_voice["tts_provider"],
        "conversation_provider": resolved_voice["conversation_provider"],
        "one_shot_tts_provider": effective["one_shot_tts_provider"],
        "settings": {
            "voice": resolved_voice,
        },
        "effective_voice_settings": effective,
    })


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Sentinel):
        return None
    return value
