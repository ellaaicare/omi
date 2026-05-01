"""
Ella conversation correction endpoints.

This router is intentionally registered through the Ella extension hook instead
of the upstream OMI conversation router so future upstream syncs do not have to
carry this custom app contract as a core patch.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import AliasChoices, BaseModel, Field, field_validator

import database.conversations as conversations_db
from database._client import db
from ella.config import ELLA_CONFIG
from utils.other import endpoints as auth

logger = logging.getLogger(__name__)

router = APIRouter(tags=["conversation-corrections"])


class SummaryContext(BaseModel):
    title: Optional[str] = None
    overview: Optional[str] = None
    app_summary: Optional[str] = None


class ConversationCorrectionRequest(BaseModel):
    correction_text: str = Field(
        ...,
        min_length=1,
        max_length=4000,
        validation_alias=AliasChoices("correction_text", "text"),
        serialization_alias="correction_text",
    )
    source: str = Field(default="ios", min_length=1, max_length=64)
    summary_context: SummaryContext = Field(default_factory=SummaryContext)

    @field_validator("correction_text")
    @classmethod
    def _strip_correction_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("correction_text cannot be blank")
        return stripped


class ConversationCorrectionResponse(BaseModel):
    correction_id: str
    conversation_id: str
    trace_id: str
    status: str
    queued: bool


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _structured_summary(conversation: dict[str, Any]) -> dict[str, Any]:
    structured = conversation.get("structured") or {}
    return {
        "title": structured.get("title") or conversation.get("title") or "",
        "overview": structured.get("overview") or conversation.get("overview") or "",
        "emoji": structured.get("emoji") or conversation.get("emoji") or "",
        "category": str(structured.get("category") or conversation.get("category") or "other"),
    }


def _segment_text(segment: dict[str, Any]) -> str:
    text = segment.get("text") or ""
    speaker = segment.get("speaker") or segment.get("speaker_name")
    if not speaker:
        speaker = "User" if segment.get("is_user") else "Speaker"
    return f"{speaker}: {text}".strip()


def _format_transcript(conversation: dict[str, Any]) -> str:
    segments = conversation.get("transcript_segments") or []
    lines = [_segment_text(segment) for segment in segments if (segment.get("text") or "").strip()]
    return "\n\n".join(lines)


def _correction_category(text: str, summary_context: SummaryContext) -> str:
    haystack = " ".join(
        [
            text,
            summary_context.title or "",
            summary_context.overview or "",
            summary_context.app_summary or "",
        ]
    ).lower()
    if any(word in haystack for word in ("name", "person", "speaker", "identity", "attribution")):
        return "identity"
    if any(word in haystack for word in ("tv", "podcast", "video", "movie", "background", "media")):
        return "media"
    if any(word in haystack for word in ("title", "headline")):
        return "title"
    if any(word in haystack for word in ("topic", "about", "missed", "forgot")):
        return "topic"
    return "other"


def _audit_ref(uid: str, conversation_id: str, correction_id: str):
    return (
        db.collection("users")
        .document(uid)
        .collection("conversations")
        .document(conversation_id)
        .collection("corrections")
        .document(correction_id)
    )


def _persist_correction_audit(
    uid: str,
    conversation_id: str,
    correction_id: str,
    payload: dict[str, Any],
) -> None:
    _audit_ref(uid, conversation_id, correction_id).set(payload, merge=True)


def _update_conversation_correction_state(uid: str, conversation_id: str, update_data: dict[str, Any]) -> None:
    conversations_db.update_conversation(uid, conversation_id, update_data)


def _append_correction_event(
    uid: str,
    conversation_id: str,
    correction_id: str,
    event: dict[str, Any],
) -> None:
    ref = _audit_ref(uid, conversation_id, correction_id)
    try:
        snapshot = ref.get()
        existing = snapshot.to_dict() if getattr(snapshot, "exists", False) else {}
    except Exception:
        existing = {}
    events = list((existing or {}).get("events") or [])
    events.append(event)
    ref.set({"events": events, "updated_at": event.get("at") or _now_iso()}, merge=True)


def _n8n_correction_response_is_accepted(response_body: Any) -> bool:
    if not isinstance(response_body, dict):
        return False
    if response_body.get("queued") is True or response_body.get("success") is True:
        return True
    status_value = str(response_body.get("status") or "").lower()
    return status_value in {"accepted", "ok", "processing", "queued", "success"}


async def _submit_correction_to_n8n(
    *,
    uid: str,
    conversation_id: str,
    correction_id: str,
    trace_id: str,
    request: ConversationCorrectionRequest,
    structured: dict[str, Any],
    transcript: str,
    segment_count: int,
) -> dict[str, Any]:
    webhook_url = f"{ELLA_CONFIG.n8n_base_url.rstrip('/')}/webhook/conversation-correction"
    payload = {
        "uid": uid,
        "conversation_id": conversation_id,
        "correction_id": correction_id,
        "trace_id": trace_id,
        "correction_text": request.correction_text,
        "text": request.correction_text,
        "correction_type": _correction_category(request.correction_text, request.summary_context),
        "source": request.source,
        "summary_context": request.summary_context.model_dump(),
        "current_summary": structured,
        "transcript": transcript,
        "segments_count": segment_count,
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(webhook_url, json=payload)
    try:
        response_body = response.json()
    except Exception:
        response_body = {}
    if response.status_code >= 400 and not _n8n_correction_response_is_accepted(response_body):
        response.raise_for_status()

    return {
        "n8n_webhook": "conversation-correction",
        "n8n_status_code": response.status_code,
        "n8n_response": response_body,
    }


async def _submit_conversation_correction(
    conversation_id: str,
    request: ConversationCorrectionRequest,
    uid: str = Depends(auth.get_current_user_uid),
) -> ConversationCorrectionResponse:
    conversation = conversations_db.get_conversation(uid, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if conversation.get("is_locked", False):
        raise HTTPException(status_code=402, detail="Conversation locked")

    correction_id = str(uuid.uuid4())
    trace_id = f"correction:{conversation_id}:{correction_id}"
    submitted_at = _now_iso()
    structured = _structured_summary(conversation)
    transcript = _format_transcript(conversation)
    segment_count = len(conversation.get("transcript_segments") or [])
    bootstrap_update = {}
    bootstrap_builder = getattr(conversations_db, "bootstrap_summary_versioning_update", None)
    if callable(bootstrap_builder):
        bootstrap_update = bootstrap_builder(conversation)

    submitted_state = {
        "correction_id": correction_id,
        "status": "submitted",
        "pending": True,
        "source": request.source,
        "submitted_at": submitted_at,
        "updated_at": submitted_at,
        "active_summary_version_id": bootstrap_update.get("active_summary_version_id")
        or conversation.get("active_summary_version_id"),
    }
    _update_conversation_correction_state(
        uid,
        conversation_id,
        {
            **bootstrap_update,
            "correction_state": submitted_state,
        },
    )

    audit_payload = {
        "correction_id": correction_id,
        "trace_id": trace_id,
        "uid": uid,
        "conversation_id": conversation_id,
        "status": "submitted",
        "source": request.source,
        "correction_text": request.correction_text,
        "category": _correction_category(request.correction_text, request.summary_context),
        "summary_context": request.summary_context.model_dump(),
        "current_summary": structured,
        "segment_count": segment_count,
        "created_at": submitted_at,
        "updated_at": submitted_at,
        "events": [{"stage": "submitted", "status": "ok", "at": submitted_at, "trace_id": trace_id}],
    }
    _persist_correction_audit(uid, conversation_id, correction_id, audit_payload)

    try:
        queue_result = await _submit_correction_to_n8n(
            uid=uid,
            conversation_id=conversation_id,
            correction_id=correction_id,
            trace_id=trace_id,
            request=request,
            structured=structured,
            transcript=transcript,
            segment_count=segment_count,
        )
    except Exception as exc:
        logger.exception(
            "Failed to submit conversation correction to n8n",
            extra={"uid": uid, "conversation_id": conversation_id, "correction_id": correction_id},
        )
        failed_at = _now_iso()
        _persist_correction_audit(
            uid,
            conversation_id,
            correction_id,
            {
                "status": "queue_failed",
                "updated_at": failed_at,
                "queue_error": str(exc),
            },
        )
        _append_correction_event(
            uid,
            conversation_id,
            correction_id,
            {"stage": "queue_failed", "status": "error", "at": failed_at, "trace_id": trace_id, "error": str(exc)},
        )
        _update_conversation_correction_state(
            uid,
            conversation_id,
            {
                "correction_state": {
                    "correction_id": correction_id,
                    "status": "queue_failed",
                    "pending": False,
                    "source": request.source,
                    "submitted_at": submitted_at,
                    "updated_at": failed_at,
                    "active_summary_version_id": submitted_state.get("active_summary_version_id"),
                    "error": str(exc),
                }
            },
        )
        return ConversationCorrectionResponse(
            correction_id=correction_id,
            conversation_id=conversation_id,
            trace_id=trace_id,
            status="queue_failed",
            queued=False,
        )

    queued_at = _now_iso()
    queued_state = {
        "correction_id": correction_id,
        "status": "queued",
        "pending": True,
        "source": request.source,
        "submitted_at": submitted_at,
        "updated_at": queued_at,
        "active_summary_version_id": submitted_state.get("active_summary_version_id"),
    }
    _persist_correction_audit(
        uid,
        conversation_id,
        correction_id,
        {"status": "queued", "updated_at": queued_at, "queue_result": queue_result},
    )
    _append_correction_event(
        uid,
        conversation_id,
        correction_id,
        {"stage": "queued", "status": "ok", "at": queued_at, "trace_id": trace_id, "queue_result": queue_result},
    )
    _update_conversation_correction_state(uid, conversation_id, {"correction_state": queued_state})

    return ConversationCorrectionResponse(
        correction_id=correction_id,
        conversation_id=conversation_id,
        trace_id=trace_id,
        status="queued",
        queued=True,
    )


@router.post(
    "/v1/ella/conversations/{conversation_id}/corrections",
    response_model=ConversationCorrectionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_conversation_correction_ella(
    conversation_id: str,
    request: ConversationCorrectionRequest,
    uid: str = Depends(auth.get_current_user_uid),
) -> ConversationCorrectionResponse:
    return await _submit_conversation_correction(conversation_id, request, uid)


@router.post(
    "/v1/conversations/{conversation_id}/corrections",
    response_model=ConversationCorrectionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_conversation_correction(
    conversation_id: str,
    request: ConversationCorrectionRequest,
    uid: str = Depends(auth.get_current_user_uid),
) -> ConversationCorrectionResponse:
    return await _submit_conversation_correction(conversation_id, request, uid)
