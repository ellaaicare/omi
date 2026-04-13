"""
Ella conversation correction endpoints.

This router is intentionally registered through the Ella extension hook instead
of the upstream OMI conversation router so future upstream syncs do not have to
carry this custom app contract as a core patch.
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator

import database.conversations as conversations_db
from database._client import db
from ella.config import ELLA_CONFIG
from ella.routers.resolve import resolve_user_routing
from utils.other import endpoints as auth

logger = logging.getLogger(__name__)

router = APIRouter(tags=["conversation-corrections"])


class SummaryContext(BaseModel):
    title: Optional[str] = None
    overview: Optional[str] = None
    app_summary: Optional[str] = None


class ConversationCorrectionRequest(BaseModel):
    correction_text: str = Field(..., min_length=1, max_length=4000)
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


def _correction_markdown(
    *,
    uid: str,
    conversation_id: str,
    correction_id: str,
    request: ConversationCorrectionRequest,
    structured: dict[str, Any],
    transcript: str,
    submitted_at: str,
) -> str:
    context_json = json.dumps(request.summary_context.model_dump(), indent=2, sort_keys=True)
    structured_json = json.dumps(structured, indent=2, sort_keys=True)
    return f"""# Conversation Summary Correction

- correction_id: {correction_id}
- uid: {uid}
- conversation_id: {conversation_id}
- source: {request.source}
- submitted_at: {submitted_at}

## Correction Text

{request.correction_text}

## iOS Summary Context

```json
{context_json}
```

## Current Backend Summary

```json
{structured_json}
```

## Full Transcript

{transcript or "(No transcript text was available from OMI.)"}
"""


async def _enqueue_correction(
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
    routing_info = await resolve_user_routing(uid)
    routing = (routing_info or {}).get("routing") or {}
    agent_id = routing.get("agentId")
    provision_url = (routing.get("provisionUrl") or "").rstrip("/")
    provision_token = routing.get("provisionToken") or ""

    if not agent_id or not provision_url:
        raise RuntimeError("OpenClaw routing is missing agentId or provisionUrl")

    correction_file = f"corrections/{correction_id}.md"
    submitted_at = _now_iso()
    markdown = _correction_markdown(
        uid=uid,
        conversation_id=conversation_id,
        correction_id=correction_id,
        request=request,
        structured=structured,
        transcript=transcript,
        submitted_at=submitted_at,
    )
    headers = {}
    if provision_token:
        headers = {"Authorization": f"Bearer {provision_token}", "x-api-key": provision_token}

    async with httpx.AsyncClient(timeout=20.0) as client:
        write_resp = await client.put(
            f"{provision_url}/workspace/{agent_id}/write",
            headers=headers,
            json={"filepath": correction_file, "content": markdown},
        )
        write_resp.raise_for_status()

        observe_resp = await client.post(
            f"{provision_url}/workspace/{agent_id}/observe",
            headers=headers,
            json={
                "trigger": "correction_submitted",
                "turn_count": 0,
                "conversation_id": conversation_id,
                "uid": uid,
                "omi_uid": uid,
                "title": structured.get("title") or "",
                "emoji": structured.get("emoji") or "",
                "category": structured.get("category") or "other",
                "segments_count": segment_count,
                "correction_id": correction_id,
                "correction_text": request.correction_text,
                "correction_source": request.source,
                "correction_file": correction_file,
                "summary_context": request.summary_context.model_dump(),
                "trace_id": trace_id,
            },
        )
        observe_resp.raise_for_status()

        n8n_status = "accepted"
        try:
            n8n_resp = await client.post(
                f"{ELLA_CONFIG.n8n_base_url.rstrip('/')}/webhook/reprocess-queue",
                json={
                    "uid": uid,
                    "conversations": [
                        {
                            "conversation_id": conversation_id,
                            "reason": f"iOS summary correction {correction_id}",
                            "correction_id": correction_id,
                        }
                    ],
                },
            )
            n8n_resp.raise_for_status()
        except Exception as exc:
            logger.warning(
                "Correction queued in OpenClaw but n8n reprocess-queue webhook failed",
                extra={"uid": uid, "conversation_id": conversation_id, "correction_id": correction_id},
            )
            n8n_status = f"failed: {exc}"

    return {
        "agent_id": agent_id,
        "correction_file": correction_file,
        "provision_observe": "accepted",
        "n8n_reprocess_queue": n8n_status,
    }


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
        queue_result = await _enqueue_correction(
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
            "Failed to enqueue conversation correction",
            extra={"uid": uid, "conversation_id": conversation_id, "correction_id": correction_id},
        )
        _persist_correction_audit(
            uid,
            conversation_id,
            correction_id,
            {
                "status": "queue_failed",
                "updated_at": _now_iso(),
                "queue_error": str(exc),
            },
        )
        _append_correction_event(
            uid,
            conversation_id,
            correction_id,
            {"stage": "queue_failed", "status": "error", "at": _now_iso(), "trace_id": trace_id, "error": str(exc)},
        )
        return ConversationCorrectionResponse(
            correction_id=correction_id,
            conversation_id=conversation_id,
            trace_id=trace_id,
            status="queue_failed",
            queued=False,
        )

    queued_at = _now_iso()
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

    return ConversationCorrectionResponse(
        correction_id=correction_id,
        conversation_id=conversation_id,
        trace_id=trace_id,
        status="queued",
        queued=True,
    )
