"""Canonical ledger adapter for OMI enriched conversations."""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

import requests

CANONICAL_EVENTS_URL = os.getenv("ELLA_CANONICAL_EVENTS_URL", "http://127.0.0.1:8000/v1/ella/events")
CANONICAL_OMI_WRITE_ENABLED = os.getenv("ELLA_CANONICAL_OMI_WRITE_ENABLED", "true").lower() == "true"
CANONICAL_OMI_TIMEOUT = float(os.getenv("ELLA_CANONICAL_OMI_TIMEOUT", "5"))


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _model_to_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "dict"):
        return value.dict()
    return {}


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "model_dump"):
        return _json_safe(value.model_dump(mode="json"))
    if hasattr(value, "dict"):
        return _json_safe(value.dict())
    enum_value = _enum_value(value)
    if enum_value is not value:
        return _json_safe(enum_value)
    return value


def _object_get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _structured_dict(conversation: Any) -> dict[str, Any]:
    structured = _object_get(conversation, "structured") or {}
    data = _model_to_dict(structured)
    if not data and isinstance(structured, dict):
        data = dict(structured)
    if data.get("category") is not None:
        data["category"] = _enum_value(data["category"])
    return data


def _segment_dict(segment: Any) -> dict[str, Any]:
    data = _model_to_dict(segment)
    if not data and isinstance(segment, dict):
        data = dict(segment)
    return {
        "id": data.get("id"),
        "text": data.get("text") or "",
        "speaker": data.get("speaker"),
        "speaker_id": data.get("speaker_id"),
        "is_user": data.get("is_user"),
        "person_id": data.get("person_id"),
        "start": data.get("start"),
        "end": data.get("end"),
        "timestamp": data.get("timestamp"),
    }


def _transcript_segments(conversation: Any) -> list[dict[str, Any]]:
    segments = _object_get(conversation, "transcript_segments") or []
    return [_segment_dict(segment) for segment in segments]


def _summary_versions(conversation: Any) -> list[dict[str, Any]]:
    versions = _object_get(conversation, "summary_versions") or []
    return [_model_to_dict(version) if not isinstance(version, dict) else dict(version) for version in versions]


def _active_summary_version_id(conversation: Any) -> Optional[str]:
    value = _object_get(conversation, "active_summary_version_id")
    return str(value) if value else None


def _summary_text(structured: dict[str, Any]) -> str:
    title = str(structured.get("title") or "").strip()
    overview = str(structured.get("overview") or "").strip()
    if title and overview:
        return f"{title}\n\n{overview}"
    return title or overview


def build_omi_canonical_event(
    uid: str,
    conversation: Any,
    *,
    summary_source: str = "observer",
    summary_kind: str = "observer_enriched",
    trace_id: Optional[str] = None,
) -> dict[str, Any]:
    """Build one idempotent canonical event for the active OMI summary."""
    conversation_id = str(_object_get(conversation, "id") or "")
    if not conversation_id:
        raise ValueError("conversation id is required")

    structured = _structured_dict(conversation)
    started_at = _object_get(conversation, "started_at") or _object_get(conversation, "created_at")
    if not started_at:
        started_at = datetime.now(timezone.utc)
    finished_at = _object_get(conversation, "finished_at")
    active_summary_version_id = _active_summary_version_id(conversation)
    transcript_segments = _transcript_segments(conversation)

    event = {
        "uid": uid,
        "canonical_identity": uid,
        "event_id": f"omi:{conversation_id}:summary",
        "session_id": conversation_id,
        "channel": "omi",
        "provider": "omi-backend",
        "role": "user",
        "text": _summary_text(structured),
        "started_at": _iso(started_at),
        "ended_at": _iso(finished_at),
        "privacy_scope": "user_private",
        "scan_policy": "none",
        "source_ref": {
            "source_identity": f"omi:{conversation_id}",
            "conversation_id": conversation_id,
            "active_summary_version_id": active_summary_version_id,
        },
        "metadata": {
            "adapter": "omi-enriched-conversation",
            "summary_source": summary_source,
            "summary_kind": summary_kind,
            "trace_id": trace_id,
            "structured": structured,
            "summary_versions": _summary_versions(conversation),
            "active_summary_version_id": active_summary_version_id,
            "enrichment_state": _model_to_dict(_object_get(conversation, "enrichment_state")),
            "internal_assessment": _model_to_dict(_object_get(conversation, "internal_assessment")),
            "ella_tags": _object_get(conversation, "ella_tags") or [],
            "ella_signal": _model_to_dict(_object_get(conversation, "ella_signal")),
            "transcript_segments": transcript_segments,
            "segment_count": len(transcript_segments),
            "created_at": _iso(_object_get(conversation, "created_at")),
            "source": _enum_value(_object_get(conversation, "source")),
            "status": _enum_value(_object_get(conversation, "status")),
        },
    }
    return _json_safe(event)


def write_omi_canonical_event(
    uid: str,
    conversation: Any,
    *,
    summary_source: str = "observer",
    summary_kind: str = "observer_enriched",
    trace_id: Optional[str] = None,
    timeout: Optional[float] = None,
) -> dict[str, Any]:
    """Best-effort synchronous write to the local canonical ledger endpoint."""
    if not CANONICAL_OMI_WRITE_ENABLED:
        return {"ok": False, "skipped": True, "reason": "disabled"}
    ledger_token = os.getenv("ELLA_EVENT_LEDGER_TOKEN", "").strip()
    if not ledger_token:
        raise RuntimeError("canonical_events_auth_not_configured")

    event = build_omi_canonical_event(
        uid,
        conversation,
        summary_source=summary_source,
        summary_kind=summary_kind,
        trace_id=trace_id,
    )
    started = time.time()
    response = requests.post(
        CANONICAL_EVENTS_URL,
        json={"events": [event]},
        headers={"Authorization": f"Bearer {ledger_token}", "Content-Type": "application/json"},
        timeout=timeout if timeout is not None else CANONICAL_OMI_TIMEOUT,
    )
    elapsed_ms = int((time.time() - started) * 1000)
    if response.status_code >= 400:
        raise RuntimeError(f"canonical_events_http_{response.status_code}: {response.text[:200]}")
    payload = response.json()
    payload["latency_ms"] = elapsed_ms
    return payload
