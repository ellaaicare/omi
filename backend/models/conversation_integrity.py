"""Pure canonical hashing helpers shared across conversation layers."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional


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


def canonical_transcript_segments(transcript_segments: list[Any]) -> list[dict[str, Any]]:
    return [_json_safe(_segment_dict(segment)) for segment in transcript_segments]


def transcript_grounding_hash(transcript_segments: list[dict[str, Any]]) -> str:
    normalized_segments = canonical_transcript_segments(transcript_segments)
    source = json.dumps(
        normalized_segments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return "sha256:" + hashlib.sha256(source.encode("utf-8")).hexdigest()


def summary_grounding_hash(structured: dict[str, Any]) -> str:
    source = json.dumps(
        {
            "overview": str(structured.get("overview") or "").strip(),
            "title": str(structured.get("title") or "").strip(),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(source.encode("utf-8")).hexdigest()
