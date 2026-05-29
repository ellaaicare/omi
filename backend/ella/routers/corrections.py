"""
Ella conversation correction endpoints.

This router is intentionally registered through the Ella extension hook instead
of the upstream OMI conversation router so future upstream syncs do not have to
carry this custom app contract as a core patch.
"""

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import AliasChoices, BaseModel, Field, field_validator

import database.conversations as conversations_db
from database._client import db
from ella.config import ELLA_CONFIG
from ella.services import proposal_ingest
from utils.other import endpoints as auth

logger = logging.getLogger(__name__)

router = APIRouter(tags=["conversation-corrections"])

DIRECT_CORRECTION_APPLY_ENABLED = os.getenv("ELLA_CORRECTION_DIRECT_APPLY", "true").lower() not in {
    "0",
    "false",
    "no",
}
DIRECT_CORRECTION_BACKGROUND_ENABLED = os.getenv("ELLA_CORRECTION_BACKGROUND_APPLY", "true").lower() not in {
    "0",
    "false",
    "no",
}
CORRECTION_PROVIDER = os.getenv("ELLA_CORRECTION_PROVIDER", "hermes-api").strip().lower() or "hermes-api"
HERMES_CORRECTION_API_URL = os.getenv(
    "ELLA_CORRECTION_HERMES_CHAT_URL",
    os.getenv("HERMES_CHAT_COMPLETIONS_URL", "http://100.76.138.56:8642/v1/chat/completions"),
)
HERMES_CORRECTION_MODEL = os.getenv(
    "ELLA_CORRECTION_HERMES_MODEL",
    os.getenv("HERMES_CORRECTION_MODEL", "ella-plato-hermes-eval"),
)
DIRECT_CORRECTION_API_URL = os.getenv("ELLA_CORRECTION_API_URL", "https://api.x.ai/v1/chat/completions")
DIRECT_CORRECTION_MODEL = os.getenv("ELLA_CORRECTION_MODEL", "grok-4.3")
DIRECT_CORRECTION_INTERNAL_BASE_URL = os.getenv("ELLA_INTERNAL_BASE_URL", "http://127.0.0.1:8000")
DIRECT_CORRECTION_TIMEOUT_SECONDS = float(os.getenv("ELLA_CORRECTION_TIMEOUT_SECONDS", "45"))
N8N_CORRECTION_FALLBACK_ENABLED = os.getenv("ELLA_CORRECTION_N8N_FALLBACK_ENABLED", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


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
    proposal_id: Optional[str] = None


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


def _compact_text(value: str, limit: int) -> str:
    text = (value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n\n[truncated]"


def _extract_json_object(content: str) -> dict[str, Any]:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        match = JSON_OBJECT_RE.search(content)
        if not match:
            raise
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("Correction model response was not a JSON object")
    return parsed


def _legacy_correction_api_key() -> str:
    return os.getenv("ELLA_CORRECTION_API_KEY") or os.getenv("XAI_API_KEY") or os.getenv("HERMES_API_KEY") or ""


def _hermes_correction_api_key() -> str:
    return (
        os.getenv("ELLA_CORRECTION_HERMES_API_KEY") or os.getenv("API_SERVER_KEY") or os.getenv("HERMES_API_KEY") or ""
    )


def _safe_session_component(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.:-]+", "-", value).strip("-")[:160] or "unknown"


def _correction_session_id(uid: str, conversation_id: str, correction_id: str) -> str:
    return ":".join(
        [
            "correction",
            _safe_session_component(uid),
            _safe_session_component(conversation_id),
            _safe_session_component(correction_id),
        ]
    )


def _build_direct_correction_prompt(
    *,
    request: ConversationCorrectionRequest,
    structured: dict[str, Any],
    transcript: str,
    segment_count: int,
) -> str:
    return f"""You are Ella, Plato's warm companion summary correction writer.

Rewrite the OMI conversation summary using the user's correction. This is an app-visible summary, not a clinical note.

Rules:
- Return JSON only.
- overview must start with "[Ella] ".
- Keep the overview warm, specific, and useful for Plato to reread later.
- Use the correction as authoritative when it resolves identity, topic, title, or media attribution.
- Re-read the entire transcript and current summary, then produce one coherent corrected summary. Do not append or splice the correction text verbatim.
- Correction text may contain dictation errors, rough phrasing, homophones, or ASR artifacts. Infer the intended correction only when strongly supported by the transcript, current summary, or durable companion context. For example, in a transcript context, "Trang script" likely means "transcript"; when discussing a young person, "team" may mean "teen".
- When the correction resolves a person's identity, propagate that identity through all relevant references in the title and overview. Do not leave stale generic labels such as "the teen" where the resolved name should be used.
- Avoid raw speaker labels such as "Speaker 5" in user-facing summaries when a name, role, or natural description can be inferred. If a speaker is still unknown, describe the action without the raw label.
- Use durable companion context when available to identify Plato/Greg and close family members, but preserve uncertainty instead of inventing.
- Preserve useful details from the transcript and current summary when they do not conflict with the correction.
- Do not invent unsupported details.
- title must be short and contain no markdown.
- category should be one of: personal, family, education, health, technology, work, business, finance, legal, media, music, news, travel, other.
- Include ella_tags and ella_signal for downstream ranking.

User correction:
{request.correction_text}

Correction source: {request.source}
Correction category: {_correction_category(request.correction_text, request.summary_context)}
Segment count: {segment_count}

Current structured summary:
{json.dumps(structured, ensure_ascii=False, indent=2)}

iOS summary context:
{request.summary_context.model_dump_json(indent=2)}

Transcript:
{_compact_text(transcript, 22000)}

Return exactly:
{{
  "title": "short title",
  "overview": "[Ella] corrected warm summary",
  "emoji": "one emoji",
  "category": "category",
  "ella_tags": ["omi"],
  "ella_signal": {{
    "salience": "low|medium|high",
    "memory_promotion": "none|candidate|promoted",
    "noise_level": "none|low|medium|high",
    "contains_media": false,
    "contains_user_speech": true,
    "guardian_relevant": false
  }}
}}
"""


def _normalize_direct_summary(result: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    title = str(result.get("title") or fallback.get("title") or "Corrected Conversation").strip()
    overview = str(result.get("overview") or fallback.get("overview") or "").strip()
    if not overview:
        raise ValueError("Correction model response missing overview")
    if not overview.startswith("[Ella] "):
        overview = "[Ella] " + overview.removeprefix("[Ella]").strip()

    emoji = str(result.get("emoji") or fallback.get("emoji") or "🪽").strip()[:4] or "🪽"
    category = str(result.get("category") or fallback.get("category") or "other").strip().lower()
    tags = result.get("ella_tags")
    if not isinstance(tags, list):
        tags = ["omi"]
    tags = [str(tag).strip().lower() for tag in tags if str(tag or "").strip()]
    if "omi" not in tags:
        tags.insert(0, "omi")
    if "correction" not in tags:
        tags.append("correction")

    signal = result.get("ella_signal")
    if not isinstance(signal, dict):
        signal = {}

    return {
        "title": title,
        "overview": overview,
        "emoji": emoji,
        "category": category,
        "ella_tags": tags[:12],
        "ella_signal": {
            "salience": str(signal.get("salience") or "medium"),
            "memory_promotion": str(signal.get("memory_promotion") or "none"),
            "noise_level": str(signal.get("noise_level") or "low"),
            "contains_media": bool(signal.get("contains_media", False)),
            "contains_user_speech": bool(signal.get("contains_user_speech", True)),
            "guardian_relevant": bool(signal.get("guardian_relevant", False)),
        },
    }


async def _generate_corrected_summary(
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
    prompt = _build_direct_correction_prompt(
        request=request,
        structured=structured,
        transcript=transcript,
        segment_count=segment_count,
    )
    provider = CORRECTION_PROVIDER
    if provider == "hermes-api":
        api_key = _hermes_correction_api_key()
        if not api_key:
            raise RuntimeError("No Hermes correction API key configured")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-Hermes-Session-Id": _correction_session_id(uid, conversation_id, correction_id),
            "X-Trace-Id": trace_id,
        }
        async with httpx.AsyncClient(timeout=DIRECT_CORRECTION_TIMEOUT_SECONDS) as client:
            response = await client.post(
                HERMES_CORRECTION_API_URL,
                headers=headers,
                json={
                    "model": HERMES_CORRECTION_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.1,
                    "max_tokens": 900,
                },
            )
        response.raise_for_status()
        body = response.json()
        content = body["choices"][0]["message"]["content"]
        return _normalize_direct_summary(_extract_json_object(content), structured)

    api_key = _legacy_correction_api_key()
    if not api_key:
        raise RuntimeError("No legacy correction model API key configured")
    async with httpx.AsyncClient(timeout=DIRECT_CORRECTION_TIMEOUT_SECONDS) as client:
        response = await client.post(
            DIRECT_CORRECTION_API_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": DIRECT_CORRECTION_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 800,
            },
        )
    response.raise_for_status()
    body = response.json()
    content = body["choices"][0]["message"]["content"]
    return _normalize_direct_summary(_extract_json_object(content), structured)


async def _apply_corrected_summary(
    *,
    uid: str,
    conversation_id: str,
    correction_id: str,
    trace_id: str,
    active_summary_version_id: Optional[str],
    corrected: dict[str, Any],
) -> dict[str, Any]:
    url = (
        f"{DIRECT_CORRECTION_INTERNAL_BASE_URL.rstrip('/')}"
        f"/v1/ella/conversation/{conversation_id}/summary?uid={uid}"
    )
    payload = {
        "title": corrected["title"],
        "overview": corrected["overview"],
        "emoji": corrected["emoji"],
        "category": corrected["category"],
        "summary_source": "observer",
        "summary_kind": "corrected_enriched",
        "correction_id": correction_id,
        "based_on_version_id": active_summary_version_id,
        "set_active": True,
        "trace_id": trace_id,
        "ella_tags": corrected.get("ella_tags") or ["omi", "correction"],
        "ella_signal": corrected.get("ella_signal") or {},
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.patch(url, json=payload)
    response.raise_for_status()
    return response.json()


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


def _summary_correction_claims(*, uid: str, trace_id: str) -> dict[str, Any]:
    return {
        "sub": f"omi-user:{uid}",
        "profile_uid": uid,
        "role": "self",
        "external_provider": "omi-ios-correction",
        "grant_id": "conversation-correction-widget",
        "trace_id": trace_id,
        "scopes": ["proposals:write"],
        "allowed_tools": ["conversation_correction_submit"],
    }


def _create_summary_correction_proposal(
    *,
    uid: str,
    conversation_id: str,
    correction_id: str,
    trace_id: str,
    request: ConversationCorrectionRequest,
    structured: dict[str, Any],
    transcript: str,
    segment_count: int,
    active_summary_version_id: Optional[str],
) -> Optional[str]:
    result = proposal_ingest.create_proposal(
        session_claims=_summary_correction_claims(uid=uid, trace_id=trace_id),
        tool_name="conversation_correction_submit",
        proposal_type="summary_correction",
        payload={
            "title": f"Summary correction for conversation {conversation_id}",
            "description": request.correction_text,
            "target": {
                "kind": "omi_conversation_summary",
                "conversation_id": conversation_id,
                "correction_id": correction_id,
                "active_summary_version_id": active_summary_version_id,
            },
            "evidence": [
                {
                    "kind": "current_summary",
                    "content": structured,
                },
                {
                    "kind": "summary_context",
                    "content": request.summary_context.model_dump(),
                },
                {
                    "kind": "transcript_excerpt",
                    "content": transcript[:4000],
                    "segment_count": segment_count,
                },
            ],
            "requested_change": {
                "correction_text": request.correction_text,
                "correction_type": _correction_category(request.correction_text, request.summary_context),
                "source": request.source,
            },
            "source": "conversation_correction_widget",
            "write_policy": "proposal_only",
        },
        idempotency_key=f"summary-correction:{uid}:{conversation_id}:{correction_id}",
    )
    proposal = result.get("proposal") or {}
    return proposal.get("proposal_id")


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


async def _run_direct_correction_apply(
    *,
    uid: str,
    conversation_id: str,
    correction_id: str,
    trace_id: str,
    request: ConversationCorrectionRequest,
    structured: dict[str, Any],
    transcript: str,
    segment_count: int,
    submitted_at: str,
    active_summary_version_id: Optional[str],
    proposal_id: Optional[str],
) -> ConversationCorrectionResponse:
    try:
        corrected_summary = await _generate_corrected_summary(
            uid=uid,
            conversation_id=conversation_id,
            correction_id=correction_id,
            trace_id=trace_id,
            request=request,
            structured=structured,
            transcript=transcript,
            segment_count=segment_count,
        )
        apply_result = await _apply_corrected_summary(
            uid=uid,
            conversation_id=conversation_id,
            correction_id=correction_id,
            trace_id=trace_id,
            active_summary_version_id=active_summary_version_id,
            corrected=corrected_summary,
        )
        applied_at = _now_iso()
        _persist_correction_audit(
            uid,
            conversation_id,
            correction_id,
            {
                "status": "applied",
                "updated_at": applied_at,
                "direct_apply_result": apply_result,
                "direct_apply_summary": corrected_summary,
            },
        )
        _append_correction_event(
            uid,
            conversation_id,
            correction_id,
            {
                "stage": "direct_apply_succeeded",
                "status": "ok",
                "at": applied_at,
                "trace_id": trace_id,
                "apply_result": apply_result,
            },
        )
        return ConversationCorrectionResponse(
            correction_id=correction_id,
            conversation_id=conversation_id,
            trace_id=trace_id,
            status="applied",
            queued=False,
            proposal_id=proposal_id,
        )
    except Exception as exc:
        logger.exception(
            "Direct conversation correction apply failed",
            extra={"uid": uid, "conversation_id": conversation_id, "correction_id": correction_id},
        )
        failed_at = _now_iso()
        _append_correction_event(
            uid,
            conversation_id,
            correction_id,
            {
                "stage": "direct_apply_failed",
                "status": "error",
                "at": failed_at,
                "trace_id": trace_id,
                "error": str(exc),
                "n8n_fallback_enabled": N8N_CORRECTION_FALLBACK_ENABLED,
            },
        )
        if not N8N_CORRECTION_FALLBACK_ENABLED:
            _persist_correction_audit(
                uid,
                conversation_id,
                correction_id,
                {
                    "status": "direct_apply_failed",
                    "updated_at": failed_at,
                    "direct_apply_error": str(exc),
                },
            )
            _update_conversation_correction_state(
                uid,
                conversation_id,
                {
                    "correction_state": {
                        "status": "direct_apply_failed",
                        "pending": False,
                        "correction_id": correction_id,
                        "trace_id": trace_id,
                        "submitted_at": submitted_at,
                        "updated_at": failed_at,
                        "error": str(exc),
                    }
                },
            )
            return ConversationCorrectionResponse(
                correction_id=correction_id,
                conversation_id=conversation_id,
                trace_id=trace_id,
                status="direct_apply_failed",
                queued=False,
                proposal_id=proposal_id,
            )
        raise


async def _submit_conversation_correction(
    conversation_id: str,
    request: ConversationCorrectionRequest,
    background_tasks: Optional[BackgroundTasks] = None,
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

    proposal_id = None
    try:
        proposal_id = _create_summary_correction_proposal(
            uid=uid,
            conversation_id=conversation_id,
            correction_id=correction_id,
            trace_id=trace_id,
            request=request,
            structured=structured,
            transcript=transcript,
            segment_count=segment_count,
            active_summary_version_id=submitted_state.get("active_summary_version_id"),
        )
        if proposal_id:
            _persist_correction_audit(
                uid,
                conversation_id,
                correction_id,
                {
                    "proposal_id": proposal_id,
                    "updated_at": _now_iso(),
                },
            )
            _append_correction_event(
                uid,
                conversation_id,
                correction_id,
                {
                    "stage": "proposal_created",
                    "status": "ok",
                    "at": _now_iso(),
                    "trace_id": trace_id,
                    "proposal_id": proposal_id,
                },
            )
    except Exception as exc:
        logger.exception(
            "Failed to create summary correction proposal",
            extra={"uid": uid, "conversation_id": conversation_id, "correction_id": correction_id},
        )
        _append_correction_event(
            uid,
            conversation_id,
            correction_id,
            {"stage": "proposal_failed", "status": "error", "at": _now_iso(), "trace_id": trace_id, "error": str(exc)},
        )

    if DIRECT_CORRECTION_APPLY_ENABLED:
        direct_apply_kwargs = {
            "uid": uid,
            "conversation_id": conversation_id,
            "correction_id": correction_id,
            "trace_id": trace_id,
            "request": request,
            "structured": structured,
            "transcript": transcript,
            "segment_count": segment_count,
            "submitted_at": submitted_at,
            "active_summary_version_id": submitted_state.get("active_summary_version_id"),
            "proposal_id": proposal_id,
        }
        if DIRECT_CORRECTION_BACKGROUND_ENABLED and background_tasks is not None:
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
                {
                    "status": "queued",
                    "updated_at": queued_at,
                    "queue_result": {"mode": "background_direct_apply"},
                },
            )
            _append_correction_event(
                uid,
                conversation_id,
                correction_id,
                {
                    "stage": "direct_apply_queued",
                    "status": "ok",
                    "at": queued_at,
                    "trace_id": trace_id,
                    "queue_result": {"mode": "background_direct_apply"},
                },
            )
            _update_conversation_correction_state(uid, conversation_id, {"correction_state": queued_state})
            background_tasks.add_task(_run_direct_correction_apply, **direct_apply_kwargs)
            return ConversationCorrectionResponse(
                correction_id=correction_id,
                conversation_id=conversation_id,
                trace_id=trace_id,
                status="queued",
                queued=True,
                proposal_id=proposal_id,
            )
        try:
            return await _run_direct_correction_apply(**direct_apply_kwargs)
        except Exception as exc:
            if not N8N_CORRECTION_FALLBACK_ENABLED:
                return ConversationCorrectionResponse(
                    correction_id=correction_id,
                    conversation_id=conversation_id,
                    trace_id=trace_id,
                    status="direct_apply_failed",
                    queued=False,
                    proposal_id=proposal_id,
                )

    if not N8N_CORRECTION_FALLBACK_ENABLED:
        skipped_at = _now_iso()
        _persist_correction_audit(
            uid,
            conversation_id,
            correction_id,
            {
                "status": "direct_apply_disabled",
                "updated_at": skipped_at,
                "queue_error": "n8n correction fallback disabled",
            },
        )
        _append_correction_event(
            uid,
            conversation_id,
            correction_id,
            {
                "stage": "fallback_skipped",
                "status": "skipped",
                "at": skipped_at,
                "trace_id": trace_id,
                "reason": "n8n correction fallback disabled",
            },
        )
        _update_conversation_correction_state(
            uid,
            conversation_id,
            {
                "correction_state": {
                    "status": "direct_apply_disabled",
                    "pending": False,
                    "correction_id": correction_id,
                    "trace_id": trace_id,
                    "submitted_at": submitted_at,
                    "updated_at": skipped_at,
                    "error": "n8n correction fallback disabled",
                }
            },
        )
        return ConversationCorrectionResponse(
            correction_id=correction_id,
            conversation_id=conversation_id,
            trace_id=trace_id,
            status="direct_apply_disabled",
            queued=False,
            proposal_id=proposal_id,
        )

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
            proposal_id=proposal_id,
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
        proposal_id=proposal_id,
    )


@router.post(
    "/v1/ella/conversations/{conversation_id}/corrections",
    response_model=ConversationCorrectionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_conversation_correction_ella(
    conversation_id: str,
    request: ConversationCorrectionRequest,
    background_tasks: BackgroundTasks,
    uid: str = Depends(auth.get_current_user_uid),
) -> ConversationCorrectionResponse:
    return await _submit_conversation_correction(conversation_id, request, background_tasks, uid)


@router.post(
    "/v1/conversations/{conversation_id}/corrections",
    response_model=ConversationCorrectionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_conversation_correction(
    conversation_id: str,
    request: ConversationCorrectionRequest,
    background_tasks: BackgroundTasks,
    uid: str = Depends(auth.get_current_user_uid),
) -> ConversationCorrectionResponse:
    return await _submit_conversation_correction(conversation_id, request, background_tasks, uid)
