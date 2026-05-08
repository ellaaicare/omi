from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from google.cloud.firestore_v1 import SERVER_TIMESTAMP
from google.cloud.firestore_v1.transforms import Sentinel

from database._client import db


def _settings_collection(uid: str):
    return db.collection("users").document(uid).collection("app_settings")


def get_voice_settings(uid: str) -> dict[str, Any]:
    if not uid:
        return {}
    doc = _settings_collection(uid).document("voice").get()
    if not getattr(doc, "exists", False):
        return {}
    data = doc.to_dict() or {}
    return _json_safe(data)


def save_voice_settings(uid: str, voice: dict[str, Any]) -> dict[str, Any]:
    if not uid:
        raise ValueError("uid required")
    data = {
        **voice,
        "updated_at": voice.get("updated_at") or datetime.now(timezone.utc).isoformat(),
        "server_updated_at": SERVER_TIMESTAMP,
    }
    _settings_collection(uid).document("voice").set(data, merge=True)

    # Mirror the effective user-facing settings onto the user doc for legacy
    # operators/tools that inspect users/{uid}.settings without subcollections.
    db.collection("users").document(uid).set({"settings": {"voice": data}}, merge=True)
    return _json_safe(data)


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
