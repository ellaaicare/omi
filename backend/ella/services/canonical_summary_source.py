"""Canonical owner-bound source contract for Ella summary compare-and-set."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

ELLA_CANONICAL_SOURCE_CONTRACT = "ella-canonical-source-v1"


def _field(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _compact_text(value: str, max_chars: int) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    return text if len(text) <= max_chars else text[: max_chars - 1].rstrip() + "…"


def conversation_data_payload(*, uid: str, conversation_id: str, conversation: Any) -> dict[str, Any]:
    """Build the exact owner-scoped payload consumed by Hermes materialization."""
    segments = _field(conversation, "transcript_segments", []) or []
    transcript_parts: list[str] = []
    for segment in segments:
        speaker = "User" if _field(segment, "is_user", False) else (_field(segment, "speaker") or "Other")
        transcript_parts.append(f"{speaker}: {_field(segment, 'text', '')}")

    structured_source = _field(conversation, "structured", {}) or {}
    structured: dict[str, Any] = {}
    if structured_source:
        structured = {
            "title": _field(structured_source, "title"),
            "overview": _field(structured_source, "overview"),
            "emoji": _field(structured_source, "emoji"),
            "category": _field(structured_source, "category"),
        }
        category = structured.get("category")
        if category is not None and hasattr(category, "value"):
            structured["category"] = category.value

    return {
        "conversation_id": conversation_id,
        "uid": uid,
        "transcript": "\n\n".join(transcript_parts),
        "segment_count": len(segments),
        "structured": structured,
        "started_at": str(_field(conversation, "started_at", "") or ""),
        "finished_at": str(_field(conversation, "finished_at", "") or ""),
    }


def canonical_source_from_payload(payload: dict[str, Any], *, uid: str, conversation_id: str) -> dict[str, Any]:
    """Normalize a backend data payload exactly as the Hermes v1 contract does."""
    structured = payload.get("structured") if isinstance(payload.get("structured"), dict) else {}
    return {
        "type": "omi-conversation",
        "conversation_id": conversation_id,
        "uid": uid,
        "started_at": str(payload.get("started_at") or ""),
        "finished_at": str(payload.get("finished_at") or ""),
        "title": _compact_text(str(structured.get("title") or payload.get("title") or "OMI conversation"), 180),
        "overview": _compact_text(str(structured.get("overview") or payload.get("overview") or ""), 4000),
        "emoji": _compact_text(str(structured.get("emoji") or payload.get("emoji") or "🪽"), 8),
        "category": _compact_text(str(structured.get("category") or payload.get("category") or "other"), 64),
        "segment_count": int(payload.get("segment_count") or 0),
        "transcript": str(payload.get("transcript") or "").strip(),
    }


def canonical_source_from_conversation(*, uid: str, conversation_id: str, conversation: Any) -> dict[str, Any]:
    return canonical_source_from_payload(
        conversation_data_payload(uid=uid, conversation_id=conversation_id, conversation=conversation),
        uid=uid,
        conversation_id=conversation_id,
    )


def canonical_source_bytes(source: dict[str, Any]) -> bytes:
    return json.dumps(source, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def canonical_source_sha256(source: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_source_bytes(source)).hexdigest()
