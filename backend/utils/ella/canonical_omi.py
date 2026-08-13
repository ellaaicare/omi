"""Canonical ledger adapter for OMI enriched conversations."""

from __future__ import annotations

import hashlib
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

import requests
from models.conversation_integrity import (
    canonical_transcript_segments,
    summary_grounding_hash,
    transcript_grounding_hash,
)
from utils.ella.canonical_auth import canonical_event_service_headers

CANONICAL_EVENTS_URL = os.getenv("ELLA_CANONICAL_EVENTS_URL", "http://127.0.0.1:8000/v1/ella/events")
CANONICAL_OMI_WRITE_ENABLED = os.getenv("ELLA_CANONICAL_OMI_WRITE_ENABLED", "true").lower() == "true"
CANONICAL_OMI_TIMEOUT = float(os.getenv("ELLA_CANONICAL_OMI_TIMEOUT", "5"))
TODAY_CARD_GROUNDING_CONTRACT_VERSION = "ella.today_card.semantic-grounding.v1"
TODAY_CARD_GROUNDING_ATTESTER = "hermes_cloud_grounding_verifier"
TODAY_CARD_PARALLEL_GROUNDING_ATTESTER = "hermes_parallel_grounding_verifier"
TODAY_CARD_GROUNDING_PROFILES = {
    "hermes_cloud": {
        "attester": TODAY_CARD_GROUNDING_ATTESTER,
        "policy_version": "hermes-cloud-grounding-verifier-v1",
        "identity_fields": (
            "runtime_interaction_id",
            "canonical_assistant_event_id",
            "verifier_runtime_interaction_id",
            "verifier_canonical_assistant_event_id",
        ),
    },
    "hermes_parallel": {
        "attester": TODAY_CARD_PARALLEL_GROUNDING_ATTESTER,
        "policy_version": "hermes-parallel-grounding-verifier-v1",
        "identity_fields": (
            "summary_request_id",
            "summary_response_id",
            "verifier_request_id",
            "verifier_response_id",
        ),
    },
}


def today_card_grounding_profile(summary_source: Any) -> Optional[dict[str, Any]]:
    profile = TODAY_CARD_GROUNDING_PROFILES.get(str(summary_source or ""))
    return dict(profile) if profile else None


def today_card_grounding_identity_is_valid(receipt: dict[str, Any], summary_source: Any) -> bool:
    profile = today_card_grounding_profile(summary_source)
    if profile is None:
        return False
    fields = profile["identity_fields"]
    values = [str(receipt.get(field) or "").strip() for field in fields]
    if any(not value for value in values):
        return False
    return len(set(values)) == len(values)


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
    segment_id = data.get("id")
    return {
        "id": str(segment_id) if segment_id is not None else None,
        "text": data.get("text") or "",
        "speaker": data.get("speaker"),
        "speaker_id": data.get("speaker_id"),
        "is_user": bool(data.get("is_user")),
        "person_id": data.get("person_id"),
        "start": data.get("start"),
        "end": data.get("end"),
        "timestamp": data.get("timestamp"),
    }


def _transcript_segments(conversation: Any) -> list[dict[str, Any]]:
    segments = _object_get(conversation, "transcript_segments") or []
    return canonical_transcript_segments(segments)


def _today_card_grounding(
    uid: str,
    conversation: Any,
    transcript_segments: list[dict[str, Any]],
    *,
    structured: dict[str, Any],
    source_version_id: Optional[str],
) -> dict[str, Any]:
    enrichment_state = _model_to_dict(_object_get(conversation, "enrichment_state"))
    receipt = enrichment_state.get("today_card_grounding")
    if not isinstance(receipt, dict):
        return {}
    matching_versions = [
        version
        for version in _summary_versions(conversation)
        if str(version.get("id") or "") == str(source_version_id or "")
    ]
    if len(matching_versions) != 1:
        return {}
    active_version = matching_versions[0]
    grounding_profile = today_card_grounding_profile(active_version.get("source"))
    if (
        str(active_version.get("title") or "").strip() != str(structured.get("title") or "").strip()
        or str(active_version.get("overview") or "").strip() != str(structured.get("overview") or "").strip()
        or active_version.get("kind") != "hermes_enriched"
        or grounding_profile is None
    ):
        return {}
    expected_transcript_hash = transcript_grounding_hash(transcript_segments)
    expected_summary_hash = summary_grounding_hash(structured)
    quote_hashes = receipt.get("supporting_quote_hashes")
    if not isinstance(quote_hashes, list) or not quote_hashes:
        return {}
    valid_quote_hashes = all(
        isinstance(value, str)
        and value.startswith("sha256:")
        and len(value.removeprefix("sha256:")) == 64
        and all(character in "0123456789abcdef" for character in value.removeprefix("sha256:"))
        for value in quote_hashes
    )
    if not valid_quote_hashes:
        return {}
    if not (
        receipt.get("contract_version") == TODAY_CARD_GROUNDING_CONTRACT_VERSION
        and receipt.get("attester") == grounding_profile["attester"]
        and receipt.get("semantic_outcome") == "supported"
        and receipt.get("source_version_id") == source_version_id
        and receipt.get("transcript_hash") == expected_transcript_hash
        and receipt.get("summary_hash") == expected_summary_hash
        and receipt.get("owner_hash") == "sha256:" + hashlib.sha256(uid.encode("utf-8")).hexdigest()
        and receipt.get("conversation_id_hash")
        == "sha256:" + hashlib.sha256(str(_object_get(conversation, "id") or "").encode("utf-8")).hexdigest()
        and today_card_grounding_identity_is_valid(receipt, active_version.get("source"))
        and receipt.get("policy_version") == grounding_profile["policy_version"]
    ):
        return {}
    return dict(receipt)


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
    today_card_grounding = _today_card_grounding(
        uid,
        conversation,
        transcript_segments,
        structured=structured,
        source_version_id=active_summary_version_id,
    )

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
            "today_card": {"grounding": today_card_grounding},
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
        headers={"Content-Type": "application/json", **canonical_event_service_headers(uid)},
        timeout=timeout if timeout is not None else CANONICAL_OMI_TIMEOUT,
    )
    elapsed_ms = int((time.time() - started) * 1000)
    if response.status_code >= 400:
        raise RuntimeError(f"canonical_events_http_{response.status_code}: {response.text[:200]}")
    payload = response.json()
    payload["latency_ms"] = elapsed_ms
    return payload
