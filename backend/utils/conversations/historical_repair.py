from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from models.conversation import Conversation

REPAIRABLE_LONG_DISCARDED_STATUSES = {"completed", "processing", "failed"}
DEFAULT_STALE_PROCESSING_AFTER = timedelta(hours=6)


def structured_has_summary_content(structured: Dict[str, Any]) -> bool:
    return any(str(structured.get(field) or "").strip() for field in ("title", "overview"))


def conversation_transcript_metrics(conversation: Conversation) -> tuple[int, int]:
    transcript = " ".join(segment.text or "" for segment in conversation.transcript_segments)
    return len(conversation.transcript_segments), len(transcript)


def conversation_repair_metadata(conversation: Conversation) -> Dict[str, Any]:
    segment_count, transcript_chars = conversation_transcript_metrics(conversation)
    structured = conversation.structured.model_dump() if conversation.structured else {}
    return {
        "conversation_id": conversation.id,
        "created_at": conversation.created_at,
        "started_at": conversation.started_at,
        "finished_at": conversation.finished_at,
        "status": conversation.status.value if conversation.status else None,
        "discarded": conversation.discarded,
        "structured_empty": not structured_has_summary_content(structured),
        "title_chars": len(structured.get("title") or ""),
        "overview_chars": len(structured.get("overview") or ""),
        "category": str(getattr(structured.get("category"), "value", structured.get("category") or "")),
        "segment_count": segment_count,
        "transcript_chars": transcript_chars,
        "processing_error": conversation.processing_error,
    }


def _is_stale_processing(conversation: Conversation, *, now: datetime, stale_after: timedelta) -> bool:
    reference = conversation.finished_at or conversation.created_at or conversation.started_at
    if reference is None:
        return False
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    return now - reference >= stale_after


def is_long_discarded_summary_failure(
    conversation: Conversation,
    min_transcript_chars: int = 25_000,
    *,
    now: datetime | None = None,
    stale_processing_after: timedelta = DEFAULT_STALE_PROCESSING_AFTER,
) -> bool:
    metadata = conversation_repair_metadata(conversation)
    status = metadata["status"]
    if status == "processing":
        now = now or datetime.now(timezone.utc)
        if not _is_stale_processing(conversation, now=now, stale_after=stale_processing_after):
            return False
    return (
        metadata["discarded"] is True
        and status in REPAIRABLE_LONG_DISCARDED_STATUSES
        and metadata["structured_empty"] is True
        and metadata["transcript_chars"] >= min_transcript_chars
    )
