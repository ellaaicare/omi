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
from enum import Enum
from typing import Any, Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import AliasChoices, BaseModel, Field, field_validator

import database.conversations as conversations_db
from database._client import db
from database.auth import get_user_from_uid
from ella.config import ELLA_CONFIG
from ella.routers.canonical_events import CanonicalEventIn, PostgresCanonicalEventStore
from ella.services.correction_propagation import propagation_run_to_dict, run_correction_propagation
from ella.services.hermes_session import canonical_omi_session_key, safe_session_component
from ella.services.ai_consent import require_current_ai_consent
from ella.services.summary_recovery import (
    SummaryProviderConfig,
    apply_summary_update,
    build_conversation_processing_retry_plan,
    extract_json_object,
    generate_summary_from_prompt,
    normalize_summary,
    recover_failed_conversation_summary,
    summary_provider_config_for_uid,
)
from ella.services.summary_writeback import ConcurrentConversationSummaryChangeError
from ella.services import proposal_ingest
from models.conversation import Conversation, ConversationStatus
from utils.ella.exact_firebase_auth import get_exact_firebase_uid

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
    os.getenv("HERMES_CHAT_COMPLETIONS_URL", ""),
)
HERMES_CORRECTION_MODEL = os.getenv(
    "ELLA_CORRECTION_HERMES_MODEL",
    os.getenv("HERMES_CORRECTION_MODEL", "ella-plato-hermes-eval"),
)
DIRECT_CORRECTION_API_URL = os.getenv("ELLA_CORRECTION_API_URL", "https://api.x.ai/v1/chat/completions")
DIRECT_CORRECTION_MODEL = os.getenv("ELLA_CORRECTION_MODEL", "grok-4.3")
DIRECT_CORRECTION_TIMEOUT_SECONDS = float(os.getenv("ELLA_CORRECTION_TIMEOUT_SECONDS", "45"))
N8N_CORRECTION_FALLBACK_ENABLED = os.getenv("ELLA_CORRECTION_N8N_FALLBACK_ENABLED", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
CORRECTION_CANONICAL_EVENT_ENABLED = os.getenv("ELLA_CORRECTION_CANONICAL_EVENT_ENABLED", "true").lower() not in {
    "0",
    "false",
    "no",
}
CORRECTION_PROPAGATION_ENABLED = os.getenv("ELLA_CORRECTION_PROPAGATION_ENABLED", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
CORRECTION_OBSERVER_WORK_ENABLED = CORRECTION_CANONICAL_EVENT_ENABLED or CORRECTION_PROPAGATION_ENABLED
CORRECTION_OBSERVER_WORK_INLINE_WITHOUT_BACKGROUND = os.getenv(
    "ELLA_CORRECTION_OBSERVER_WORK_INLINE_WITHOUT_BACKGROUND", "true"
).lower() not in {
    "0",
    "false",
    "no",
}
_canonical_event_store = PostgresCanonicalEventStore()


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


class CorrectionSummarySnapshot(BaseModel):
    title: str = ""
    overview: str = ""
    emoji: str = ""
    category: str = "other"


class ConversationCorrectionReceiptResponse(BaseModel):
    correction_id: str
    conversation_id: str
    status: str
    applied_at: Optional[datetime] = None
    undone_at: Optional[datetime] = None
    before_version_id: Optional[str] = None
    after_version_id: Optional[str] = None
    active_version_id: Optional[str] = None
    undo_version_id: Optional[str] = None
    before: CorrectionSummarySnapshot
    after: CorrectionSummarySnapshot
    propagation_status: str = "known"
    propagation_applied_count: Optional[int] = 0
    propagation_reverted_count: Optional[int] = 0


class ConversationProcessingRetryOutcome(str, Enum):
    processing = "processing"
    completed = "completed"
    failed = "failed"


class ConversationProcessingRecoveryMode(str, Enum):
    none = "none"
    full = "full"
    enrichment_only = "enrichment_only"


class RetryConversationProcessingRequest(BaseModel):
    request_id: uuid.UUID
    correction_text: Optional[str] = Field(
        default=None,
        max_length=4000,
        validation_alias=AliasChoices("correction_text", "context"),
        serialization_alias="correction_text",
    )


class RetryConversationProcessingResponse(BaseModel):
    outcome: ConversationProcessingRetryOutcome
    recovery_mode: ConversationProcessingRecoveryMode = ConversationProcessingRecoveryMode.none
    phase: Optional[str] = None
    generic_status: Optional[str] = None
    generic_vector_status: Optional[str] = None
    enrichment_status: Optional[str] = None
    vector_status: Optional[str] = None
    lease_expires_at: Optional[datetime] = None
    attempt_count: int = 0
    conversation: Conversation


class ConversationProcessingRetryPlan(BaseModel):
    conversation_id: str
    status: str
    discarded: bool
    processing_error: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    transcript_segment_count: int
    transcript_character_count: int
    transcript_sha256: str
    structured_summary_present: bool
    structured_summary_sha256: Optional[str] = None
    active_summary_version_id: Optional[str] = None
    active_summary_source: Optional[str] = None
    active_summary_kind: Optional[str] = None
    enriched_summary_present: bool
    canonical_provenance_confirmed: bool
    vector_present: bool
    vector_active_summary_version_id: Optional[str] = None
    vector_content_sha256: Optional[str] = None
    vector_matches_active_summary: bool
    recovery_mode: ConversationProcessingRecoveryMode
    retryable: bool
    reason: str
    profile_scope_sha256: str
    canonical_session_scope_sha256: str
    provider_path: list[str]
    zero_writes: bool


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
    return extract_json_object(content)


def _legacy_correction_api_key() -> str:
    return os.getenv("ELLA_CORRECTION_API_KEY") or os.getenv("XAI_API_KEY") or os.getenv("HERMES_API_KEY") or ""


def _hermes_correction_api_key() -> str:
    return (
        os.getenv("ELLA_CORRECTION_HERMES_API_KEY") or os.getenv("API_SERVER_KEY") or os.getenv("HERMES_API_KEY") or ""
    )


def _correction_session_id(uid: str, conversation_id: str, correction_id: str) -> str:
    return ":".join(
        [
            "correction",
            safe_session_component(uid),
            safe_session_component(conversation_id),
            safe_session_component(correction_id),
        ]
    )


def _correction_session_key(uid: str) -> str:
    """Stable Hermes long-term memory scope for correction/enrichment calls."""

    return canonical_omi_session_key(uid)


def _build_direct_correction_prompt(
    *,
    request: ConversationCorrectionRequest,
    structured: dict[str, Any],
    transcript: str,
    segment_count: int,
) -> str:
    return f"""You are Ella, a warm and attentive summary correction writer for the OMI companion app.

Rewrite the OMI conversation summary using the user's correction. This is an app-visible summary, not a clinical note.

Rules:
- Return JSON only.
- overview must start with "[Ella] ".
- Keep the overview warm, specific, and useful for the account holder to reread later.
- Write the overview in the third person, describing what happened. Never address the account holder directly, use a greeting or salutation, or invent a persona for them.
- title and overview must contain only descriptive summary content, not an acknowledgment, reply to the correction, or message to a person.
- Do not begin any sentence with a single capitalized word followed by a comma. Do not end a sentence with a comma followed by a standalone capitalized word. Keep time and weekday details inside the descriptive sentence instead.
- Use the correction as authoritative when it resolves identity, topic, title, or media attribution.
- Re-read the entire transcript and current summary, then produce one coherent corrected summary. Do not append or splice the correction text verbatim.
- Correction text may contain dictation errors, rough phrasing, homophones, or ASR artifacts. Infer the intended correction only when strongly supported by the transcript, current summary, or the correction text itself. For example, in a transcript context, "Trang script" likely means "transcript"; when discussing a young person, "team" may mean "teen".
- When the correction resolves a person's identity, propagate that identity through all relevant references in the title and overview. Do not leave stale generic labels such as "the teen" where the resolved name should be used.
- Avoid raw speaker labels such as "Speaker 5" in user-facing summaries when a name, role, or natural description can be inferred. If a speaker is still unknown, describe the action without the raw label.
- Only use a person's name if it appears in the transcript, the current summary, or the correction text. Never invent a name, and never carry over a name from outside this conversation.
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
    return normalize_summary(result, fallback, required_tags=("omi", "correction"))


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
    config = await summary_provider_config_for_uid(
        uid,
        SummaryProviderConfig(
            provider=CORRECTION_PROVIDER,
            hermes_url=HERMES_CORRECTION_API_URL,
            hermes_model=HERMES_CORRECTION_MODEL,
            hermes_api_key=_hermes_correction_api_key(),
            legacy_url=DIRECT_CORRECTION_API_URL,
            legacy_model=DIRECT_CORRECTION_MODEL,
            legacy_api_key=_legacy_correction_api_key(),
            timeout_seconds=DIRECT_CORRECTION_TIMEOUT_SECONDS,
        ),
    )
    return await generate_summary_from_prompt(
        prompt=prompt,
        fallback=structured,
        session_id=_correction_session_id(uid, conversation_id, correction_id),
        session_key=_correction_session_key(uid),
        trace_id=trace_id,
        required_tags=("omi", "correction"),
        config=config,
        async_client_factory=httpx.AsyncClient,
    )


async def _apply_corrected_summary(
    *,
    uid: str,
    conversation_id: str,
    correction_id: str,
    trace_id: str,
    active_summary_version_id: Optional[str],
    corrected: dict[str, Any],
) -> dict[str, Any]:
    return await apply_summary_update(
        uid=uid,
        conversation_id=conversation_id,
        trace_id=trace_id,
        active_summary_version_id=active_summary_version_id,
        summary=corrected,
        summary_kind="corrected_enriched",
        correction_id=correction_id,
    )


def _audit_ref(uid: str, conversation_id: str, correction_id: str):
    return (
        db.collection("users")
        .document(uid)
        .collection("conversations")
        .document(conversation_id)
        .collection("corrections")
        .document(correction_id)
    )


def _summary_version(conversation: dict[str, Any], version_id: Optional[str]) -> Optional[dict[str, Any]]:
    if not version_id:
        return None
    for version in conversation.get("summary_versions") or []:
        if isinstance(version, dict) and str(version.get("id") or "") == str(version_id):
            return version
    return None


def _summary_version_by_kind_and_base(
    conversation: dict[str, Any],
    *,
    kind: str,
    based_on_version_id: str,
) -> Optional[dict[str, Any]]:
    return next(
        (
            version
            for version in reversed(conversation.get("summary_versions") or [])
            if isinstance(version, dict)
            and str(version.get("kind") or "") == kind
            and str(version.get("based_on_version_id") or "") == based_on_version_id
        ),
        None,
    )


def _require_unlocked_conversation(conversation: dict[str, Any]) -> None:
    if conversation.get("is_locked", False):
        raise HTTPException(status_code=402, detail="Conversation locked")


def _correction_summary_versions(
    conversation: dict[str, Any],
    correction_id: str,
) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    corrected = next(
        (
            version
            for version in reversed(conversation.get("summary_versions") or [])
            if isinstance(version, dict) and str(version.get("correction_id") or "") == correction_id
        ),
        None,
    )
    before = _summary_version(conversation, corrected.get("based_on_version_id") if corrected else None)
    return before, corrected


def _snapshot(version: Optional[dict[str, Any]]) -> CorrectionSummarySnapshot:
    value = version or {}
    return CorrectionSummarySnapshot(
        title=str(value.get("title") or ""),
        overview=str(value.get("overview") or ""),
        emoji=str(value.get("emoji") or ""),
        category=str(value.get("category") or "other"),
    )


def _correction_propagation_counts(
    uid: str,
    conversation_id: str,
    correction_id: str,
) -> tuple[Optional[int], Optional[int], str]:
    applied = 0
    reverted = 0
    try:
        snapshots = _audit_ref(uid, conversation_id, correction_id).collection("propagation_runs").stream()
        for snapshot in snapshots:
            run = snapshot.to_dict() or {}
            applied += int(run.get("auto_applied_count") or 0)
            reverted += int(run.get("reverted_count") or 0)
    except Exception:
        logger.exception(
            "Failed to read correction propagation counts",
            extra={"uid": uid, "conversation_id": conversation_id, "correction_id": correction_id},
        )
        return None, None, "unknown"
    return applied, reverted, "known"


def _correction_receipt(
    *,
    uid: str,
    conversation_id: str,
    correction_id: str,
    conversation: Optional[dict[str, Any]] = None,
) -> ConversationCorrectionReceiptResponse:
    conversation = conversation or conversations_db.get_conversation(uid, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    _require_unlocked_conversation(conversation)
    audit_snapshot = _audit_ref(uid, conversation_id, correction_id).get()
    audit = audit_snapshot.to_dict() if getattr(audit_snapshot, "exists", False) else {}
    before, corrected = _correction_summary_versions(conversation, correction_id)
    if not audit and corrected is None:
        raise HTTPException(status_code=404, detail="Correction not found")
    raw_state = conversation.get("correction_state")
    state = (
        raw_state if isinstance(raw_state, dict) and str(raw_state.get("correction_id") or "") == correction_id else {}
    )
    status_value = str(audit.get("status") or state.get("status") or "pending")
    applied_count, reverted_count, propagation_status = _correction_propagation_counts(
        uid,
        conversation_id,
        correction_id,
    )
    corrected_id = str(corrected.get("id") or "") if corrected else ""
    undo_version = (
        _summary_version_by_kind_and_base(
            conversation,
            kind="correction_undo",
            based_on_version_id=corrected_id,
        )
        if corrected_id
        else None
    )
    return ConversationCorrectionReceiptResponse(
        correction_id=correction_id,
        conversation_id=conversation_id,
        status=status_value,
        applied_at=audit.get("applied_at"),
        undone_at=audit.get("undone_at"),
        before_version_id=(str(before.get("id") or "") or None) if before else None,
        after_version_id=corrected_id or None,
        active_version_id=str(conversation.get("active_summary_version_id") or "") or None,
        undo_version_id=(str(undo_version.get("id") or "") or None) if undo_version else None,
        before=_snapshot(before),
        after=_snapshot(corrected),
        propagation_status=propagation_status,
        propagation_applied_count=applied_count,
        propagation_reverted_count=reverted_count,
    )


async def apply_memory_reinterpretation_correction(
    *,
    uid: str,
    conversation_id: str,
    correction_id: str,
    trace_id: str,
    active_summary_version_id: str,
    correction_text: str,
    corrected_summary: dict[str, Any],
    evidence_event_ids: list[str],
    source_session_id: str,
) -> dict[str, Any]:
    """Apply one worker-confirmed proposal through the existing CAS/receipt path.

    The deterministic correction ID and trace ID make crash-after-apply replay
    idempotent. Hermes never calls this function directly; the OMI worker first
    validates exact canonical evidence and the signed starting version.
    """
    conversation = conversations_db.get_conversation(uid, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    _require_unlocked_conversation(conversation)
    _, existing = _correction_summary_versions(conversation, correction_id)
    if existing is not None:
        enrichment_state = conversation.get("enrichment_state") or {}
        if enrichment_state.get("trace_id") == trace_id and enrichment_state.get("canonical_status") != "completed":
            await apply_summary_update(
                uid=uid,
                conversation_id=conversation_id,
                trace_id=trace_id,
                active_summary_version_id=active_summary_version_id,
                summary=corrected_summary,
                summary_kind="voice_reinterpreted",
                summary_source="voice-memory-reinterpretation",
                correction_id=correction_id,
                require_canonical=True,
                require_based_on_match=False,
                preserve_generated_results=True,
            )
            conversation = conversations_db.get_conversation(uid, conversation_id) or conversation
        replayed_at = _now_iso()
        _persist_correction_audit(
            uid,
            conversation_id,
            correction_id,
            {
                "status": "applied",
                "applied_at": replayed_at,
                "updated_at": replayed_at,
                "applied_summary_version_id": str(existing.get("id") or ""),
                "idempotent_replay": True,
            },
        )
        return {
            "status": "ok",
            "conversation_id": conversation_id,
            "active_summary_version_id": str(existing.get("id") or ""),
            "idempotent_replay": True,
            "receipt": _correction_receipt(
                uid=uid,
                conversation_id=conversation_id,
                correction_id=correction_id,
                conversation=conversation,
            ).model_dump(mode="json"),
        }

    if str(conversation.get("active_summary_version_id") or "") != active_summary_version_id:
        raise ConcurrentConversationSummaryChangeError("active_summary_version_changed")

    submitted_at = _now_iso()
    _persist_correction_audit(
        uid,
        conversation_id,
        correction_id,
        {
            "correction_id": correction_id,
            "trace_id": trace_id,
            "uid": uid,
            "conversation_id": conversation_id,
            "status": "submitted",
            "source": "voice-memory-reinterpretation",
            "correction_text": correction_text,
            "evidence_event_ids": list(evidence_event_ids),
            "source_session_id": source_session_id,
            "created_at": submitted_at,
            "updated_at": submitted_at,
            "events": [
                {
                    "stage": "submitted",
                    "status": "ok",
                    "at": submitted_at,
                    "trace_id": trace_id,
                }
            ],
        },
    )
    result = await apply_summary_update(
        uid=uid,
        conversation_id=conversation_id,
        trace_id=trace_id,
        active_summary_version_id=active_summary_version_id,
        summary=corrected_summary,
        summary_kind="voice_reinterpreted",
        summary_source="voice-memory-reinterpretation",
        correction_id=correction_id,
        require_canonical=True,
        require_based_on_match=True,
        preserve_generated_results=True,
    )
    applied_at = _now_iso()
    _persist_correction_audit(
        uid,
        conversation_id,
        correction_id,
        {
            "status": "applied",
            "applied_at": applied_at,
            "updated_at": applied_at,
            "applied_summary_version_id": result.get("active_summary_version_id"),
            "source_session_id": source_session_id,
            "evidence_event_ids": list(evidence_event_ids),
        },
    )
    return {
        **result,
        "idempotent_replay": bool(result.get("idempotent_replay")),
        "receipt": _correction_receipt(
            uid=uid,
            conversation_id=conversation_id,
            correction_id=correction_id,
        ).model_dump(mode="json"),
    }


def _prepare_applied_propagation_rollbacks(
    uid: str,
    conversation_id: str,
    correction_id: str,
) -> list[dict[str, Any]]:
    try:
        snapshots = list(_audit_ref(uid, conversation_id, correction_id).collection("propagation_runs").stream())
    except Exception as exc:
        logger.exception(
            "Failed to preflight correction propagation rollback",
            extra={"uid": uid, "conversation_id": conversation_id, "correction_id": correction_id},
        )
        raise HTTPException(status_code=503, detail="Propagation rollback state is unavailable") from exc

    plan: list[dict[str, Any]] = []
    seen_related_ids: set[str] = set()
    for snapshot in snapshots:
        run = snapshot.to_dict() or {}
        auto_applied_count = int(run.get("auto_applied_count") or 0)
        if auto_applied_count <= 0:
            continue
        decisions = run.get("decisions") if isinstance(run.get("decisions"), list) else []
        applicable_indexes = [
            index
            for index, decision in enumerate(decisions)
            if isinstance(decision, dict) and decision.get("action") in {"applied", "auto_applied"}
        ]
        if len(applicable_indexes) != auto_applied_count:
            raise HTTPException(status_code=409, detail="Propagation rollback data is incomplete")
        for decision_index in applicable_indexes:
            decision = decisions[decision_index]
            rollback = decision.get("rollback_ref") if isinstance(decision.get("rollback_ref"), dict) else {}
            structured = rollback.get("structured") if isinstance(rollback.get("structured"), dict) else None
            related_id = str(decision.get("conversation_id") or "")
            applied_version_id = str(decision.get("applied_summary_version_id") or "")
            if not related_id or structured is None or not applied_version_id:
                raise HTTPException(status_code=409, detail="Propagation rollback data is incomplete")
            if related_id in seen_related_ids:
                raise HTTPException(status_code=409, detail="Propagation rollback contains duplicate targets")
            seen_related_ids.add(related_id)

            related = conversations_db.get_conversation(uid, related_id)
            if related is None:
                raise HTTPException(status_code=409, detail="Propagation rollback target is missing")
            _require_unlocked_conversation(related)

            reverted_version_id = str(decision.get("reverted_summary_version_id") or "")
            discovered_undo = _summary_version_by_kind_and_base(
                related,
                kind="correction_propagation_undo",
                based_on_version_id=applied_version_id,
            )
            if not reverted_version_id and discovered_undo is not None:
                reverted_version_id = str(discovered_undo.get("id") or "")

            current_version_id = str(related.get("active_summary_version_id") or "")
            expected_version_id = reverted_version_id or applied_version_id
            if current_version_id != expected_version_id:
                raise HTTPException(status_code=409, detail="A newer related memory update must be undone first")

            plan.append(
                {
                    "snapshot": snapshot,
                    "run": run,
                    "decisions": decisions,
                    "decision_index": decision_index,
                    "related_id": related_id,
                    "structured": structured,
                    "applied_version_id": applied_version_id,
                    "reverted_version_id": reverted_version_id,
                    "completed": bool(reverted_version_id),
                }
            )
    return plan


def _persist_propagation_rollback_progress(item: dict[str, Any]) -> None:
    decisions = item["decisions"]
    decision = decisions[item["decision_index"]]
    decision["reverted_summary_version_id"] = item["reverted_version_id"]
    decision["rollback_status"] = "reverted"
    decision["reverted_at"] = item["reverted_at"]
    reverted_count = sum(
        1
        for value in decisions
        if isinstance(value, dict)
        and value.get("action") in {"applied", "auto_applied"}
        and value.get("reverted_summary_version_id")
    )
    auto_applied_count = int(item["run"].get("auto_applied_count") or 0)
    payload: dict[str, Any] = {
        "decisions": decisions,
        "reverted_count": reverted_count,
    }
    if reverted_count == auto_applied_count:
        payload["reverted_at"] = item["reverted_at"]
    item["snapshot"].reference.set(payload, merge=True)


async def _revert_applied_propagations(
    uid: str,
    conversation_id: str,
    correction_id: str,
    *,
    rollback_plan: Optional[list[dict[str, Any]]] = None,
) -> int:
    plan = rollback_plan
    if plan is None:
        plan = _prepare_applied_propagation_rollbacks(uid, conversation_id, correction_id)
    reverted = 0
    for item in plan:
        if not item["completed"]:
            try:
                result = await apply_summary_update(
                    uid=uid,
                    conversation_id=item["related_id"],
                    trace_id=f"correction-propagation-undo:{conversation_id}:{correction_id}:{item['related_id']}",
                    active_summary_version_id=item["applied_version_id"],
                    summary=item["structured"],
                    summary_kind="correction_propagation_undo",
                    summary_source="observer",
                    require_based_on_match=True,
                    preserve_generated_results=True,
                )
            except ConcurrentConversationSummaryChangeError as exc:
                raise HTTPException(
                    status_code=409,
                    detail="A newer related memory update must be undone first",
                ) from exc
            reverted_version_id = str(result.get("active_summary_version_id") or "")
            if not reverted_version_id:
                raise HTTPException(status_code=500, detail="Propagation rollback version was not recorded")
            item["reverted_version_id"] = reverted_version_id
            item["completed"] = True
        item["reverted_at"] = _now_iso()
        _persist_propagation_rollback_progress(item)
        reverted += 1
    return reverted


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


async def _emit_canonical_correction_event(
    *,
    uid: str,
    conversation_id: str,
    correction_id: str,
    trace_id: str,
    request: ConversationCorrectionRequest,
    structured: dict[str, Any],
    submitted_at: str,
    active_summary_version_id: Optional[str],
    proposal_id: Optional[str],
) -> None:
    if not CORRECTION_CANONICAL_EVENT_ENABLED:
        return
    event = CanonicalEventIn(
        uid=uid,
        canonical_identity=uid,
        event_id=f"omi_correction:{uid}:{conversation_id}:{correction_id}:summary_correction_submitted",
        session_id=f"omi:{uid}:{conversation_id}",
        channel="omi_correction",
        provider="omi-backend",
        role="user",
        text=request.correction_text,
        started_at=submitted_at,
        privacy_scope="user_private",
        scan_policy="none",
        source_ref={
            "source_identity": f"omi_correction:{uid}:{conversation_id}:{correction_id}",
            "conversation_id": conversation_id,
            "correction_id": correction_id,
            "trace_id": trace_id,
        },
        metadata={
            "event_type": "summary_correction_submitted",
            "source": request.source,
            "correction_type": _correction_category(request.correction_text, request.summary_context),
            "summary_context": request.summary_context.model_dump(),
            "before": {
                "active_summary_version_id": active_summary_version_id,
                "current_summary": structured,
            },
            "after": {
                "correction_text": request.correction_text,
                "proposal_id": proposal_id,
                "write_policy": "proposal_then_observer",
            },
            "local_timestamps": {
                "submitted_at": submitted_at,
            },
            "durable_owner": "honcho/hermes",
        },
    )
    try:
        result = await _canonical_event_store.write_batch([event])
        _append_correction_event(
            uid,
            conversation_id,
            correction_id,
            {
                "stage": "canonical_event_emitted",
                "status": "ok",
                "at": _now_iso(),
                "trace_id": trace_id,
                "event_id": event.event_id,
                "write_result": result,
            },
        )
    except Exception as exc:
        logger.warning(
            "Failed to emit canonical correction event",
            extra={"uid": uid, "conversation_id": conversation_id, "correction_id": correction_id, "error": str(exc)},
        )
        _append_correction_event(
            uid,
            conversation_id,
            correction_id,
            {
                "stage": "canonical_event_failed",
                "status": "error",
                "at": _now_iso(),
                "trace_id": trace_id,
                "error": str(exc),
            },
        )


def _persist_correction_propagation_run(
    uid: str,
    conversation_id: str,
    correction_id: str,
    run_payload: dict[str, Any],
) -> None:
    _audit_ref(uid, conversation_id, correction_id).collection("propagation_runs").document(run_payload["run_id"]).set(
        run_payload, merge=True
    )


async def _run_correction_propagation_for_submission(
    *,
    uid: str,
    conversation_id: str,
    correction_id: str,
    trace_id: str,
    request: ConversationCorrectionRequest,
    source_conversation: dict[str, Any],
) -> None:
    if not CORRECTION_PROPAGATION_ENABLED:
        return
    try:
        run = run_correction_propagation(
            uid=uid,
            source_conversation={**source_conversation, "id": conversation_id},
            correction_id=correction_id,
            trace_id=trace_id,
            correction_text=request.correction_text,
            correction_type=_correction_category(request.correction_text, request.summary_context),
        )
        payload = propagation_run_to_dict(run)
        _persist_correction_propagation_run(uid, conversation_id, correction_id, payload)
        _append_correction_event(
            uid,
            conversation_id,
            correction_id,
            {
                "stage": "propagation_run_completed",
                "status": run.status,
                "at": _now_iso(),
                "trace_id": trace_id,
                "run_id": run.run_id,
                "candidate_count": run.candidate_count,
                "proposal_count": run.proposal_count,
                "skipped_count": run.skipped_count,
            },
        )
        _persist_correction_audit(
            uid,
            conversation_id,
            correction_id,
            {
                "propagation_run_id": run.run_id,
                "propagation_status": run.status,
                "propagation_candidate_count": run.candidate_count,
                "propagation_proposal_count": run.proposal_count,
                "updated_at": _now_iso(),
            },
        )
    except Exception as exc:
        logger.exception(
            "Correction propagation run failed",
            extra={"uid": uid, "conversation_id": conversation_id, "correction_id": correction_id},
        )
        _append_correction_event(
            uid,
            conversation_id,
            correction_id,
            {
                "stage": "propagation_run_failed",
                "status": "error",
                "at": _now_iso(),
                "trace_id": trace_id,
                "error": str(exc),
            },
        )


async def _run_correction_observer_work(
    *,
    uid: str,
    conversation_id: str,
    correction_id: str,
    trace_id: str,
    request: ConversationCorrectionRequest,
    source_conversation: dict[str, Any],
    structured: dict[str, Any],
    submitted_at: str,
    active_summary_version_id: Optional[str],
    proposal_id: Optional[str],
) -> None:
    await _emit_canonical_correction_event(
        uid=uid,
        conversation_id=conversation_id,
        correction_id=correction_id,
        trace_id=trace_id,
        request=request,
        structured=structured,
        submitted_at=submitted_at,
        active_summary_version_id=active_summary_version_id,
        proposal_id=proposal_id,
    )
    await _run_correction_propagation_for_submission(
        uid=uid,
        conversation_id=conversation_id,
        correction_id=correction_id,
        trace_id=trace_id,
        request=request,
        source_conversation=source_conversation,
    )


async def _queue_correction_observer_work(
    *,
    background_tasks: Optional[BackgroundTasks],
    uid: str,
    conversation_id: str,
    correction_id: str,
    trace_id: str,
    request: ConversationCorrectionRequest,
    source_conversation: dict[str, Any],
    structured: dict[str, Any],
    submitted_at: str,
    active_summary_version_id: Optional[str],
    proposal_id: Optional[str],
) -> None:
    if not CORRECTION_OBSERVER_WORK_ENABLED:
        return
    observer_kwargs = {
        "uid": uid,
        "conversation_id": conversation_id,
        "correction_id": correction_id,
        "trace_id": trace_id,
        "request": request,
        "source_conversation": source_conversation,
        "structured": structured,
        "submitted_at": submitted_at,
        "active_summary_version_id": active_summary_version_id,
        "proposal_id": proposal_id,
    }
    if background_tasks is not None:
        background_tasks.add_task(_run_correction_observer_work, **observer_kwargs)
        _append_correction_event(
            uid,
            conversation_id,
            correction_id,
            {
                "stage": "observer_work_queued",
                "status": "ok",
                "at": _now_iso(),
                "trace_id": trace_id,
                "queue_result": {"mode": "background_correction_observer"},
            },
        )
        return
    if CORRECTION_OBSERVER_WORK_INLINE_WITHOUT_BACKGROUND:
        await _run_correction_observer_work(**observer_kwargs)


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


class CorrectionIdentityGateError(Exception):
    """Raised when a corrected summary fails the identity/name gate."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


_PROPER_NAME_TOKEN_RE = re.compile(r"[^\W\d_]+(?:['’\-][^\W\d_]+)*", re.UNICODE)
_POSSESSIVE_SUFFIX_RE = re.compile(r"['’][sS]$")
_ELLA_OVERVIEW_PREFIX_RE = re.compile(r"^\[Ella\]\s*")

# Small, deliberately narrow stoplist of common all-caps acronyms/initialisms
# that should never be treated as a candidate identity even though every
# letter is uppercase and the token is long enough to otherwise qualify.
# Anything not on this list -- including a fictional all-caps name like
# "FABLE" -- is a candidate that must be grounded.
_ALL_CAPS_ACRONYM_STOPLIST = {
    "CEO",
    "CFO",
    "CTO",
    "FAQ",
    "ASAP",
    "TODO",
    "URL",
    "API",
    "PDF",
    "USA",
    "DIY",
    "RSVP",
    "ETA",
    "FYI",
    "VIP",
    "GPS",
    "PIN",
    "SSN",
    "ATM",
}


def _strip_possessive_suffix(token: str) -> str:
    """Drop a trailing possessive `'s`/`'s` so it isn't folded into the name.

    The token regex above deliberately joins an interior apostrophe into the
    name (e.g. "O'Fable") since that apostrophe is part of the name itself.
    A trailing possessive marker is not: "Rowan's" must still ground/collide
    with "Rowan", not a distinct token "Rowan's".
    """

    stripped = _POSSESSIVE_SUFFIX_RE.sub("", token)
    return stripped or token


def _is_name_shaped_token(token: str) -> bool:
    """Whether `token` is plausibly a person's name, Unicode-aware.

    A token is a candidate identity when its first letter is uppercase in
    any script (covers accented/non-Latin names like "Élora" as well as
    ordinary ASCII ones), including tokens that embed an apostrophe/curly
    apostrophe or hyphen (e.g. "O’Fable", "Anne-Marie") since the token
    regex above already keeps those joined into a single token. An
    all-uppercase token of plausible name length (>= 3 letters) is also a
    candidate -- e.g. a fictional "FABLE" -- unless it is on the small
    acronym stoplist above.
    """

    letters = [c for c in token if c.isalpha()]
    if not letters or not letters[0].isupper():
        return False
    if all(c.isupper() for c in letters):
        if len(letters) < 3:
            return False
        if token.upper() in _ALL_CAPS_ACRONYM_STOPLIST:
            return False
    return True


_VOCATIVE_GREETING_RE = re.compile(r"(?i)(?:^|[.!?]\s+)(?:hi|hey|hello|dear|greetings)\b,?\s*[A-Z][a-z]+")
_VOCATIVE_LEADING_ADDRESS_RE = re.compile(r"(?:^|[.!?]\s+)[A-Z][a-z]+,\s")
_VOCATIVE_TRAILING_ADDRESS_RE = re.compile(r",\s*[A-Z][a-z]+[.!?](?:\s|$)")

# "Ella" is the assistant's own persona name (required by the "[Ella] "
# overview prefix), not a personal identity that needs to be grounded
# per-account.
_PERSONA_NAME_ALLOWLIST = {"Ella"}

# Closed-class function words that are capitalized only because they open a
# sentence or a Title-Cased heading -- never plausible person names, so they
# never need grounding. Deliberately small: every OTHER capitalized token
# (any content word, including a sentence-initial one) is treated as a
# candidate identity and must be grounded. This intentionally does not rely
# on a verb allowlist to decide whether a sentence-initial capitalized word
# is a name -- a finite list of "name-indicating" verbs can always be
# bypassed by an arbitrary verb the list happens to omit (e.g. "Rowan baked
# cookies..."). Modal/auxiliary verbs that double as common first names
# (e.g. "Will", "May") are deliberately left out of this stoplist.
_CAPITALIZED_STOPWORDS = {
    "A",
    "An",
    "The",
    "And",
    "But",
    "Or",
    "Nor",
    "So",
    "Because",
    "Although",
    "While",
    "If",
    "Unless",
    "Since",
    "Though",
    "Whereas",
    "With",
    "Of",
    "In",
    "On",
    "At",
    "By",
    "To",
    "From",
    "About",
    "Over",
    "Under",
    "Between",
    "Into",
    "Onto",
    "Through",
    "During",
    "After",
    "Before",
    "Above",
    "Below",
    "Around",
    "Near",
    "Without",
    "Within",
    "Across",
    "It",
    "He",
    "She",
    "They",
    "We",
    "I",
    "You",
    "This",
    "That",
    "These",
    "Those",
    "There",
    "Here",
    "Who",
    "What",
    "When",
    "Where",
    "Why",
    "How",
    "Which",
    "Is",
    "Was",
    "Are",
    "Were",
    "Be",
    "Been",
    "Being",
    "Do",
    "Does",
    "Did",
    "Has",
    "Had",
    "Have",
}


def _extract_capitalized_tokens(text: str) -> set[str]:
    tokens = (_strip_possessive_suffix(token) for token in _PROPER_NAME_TOKEN_RE.findall(text or ""))
    return {token for token in tokens if _is_name_shaped_token(token)}


def _strip_word_punctuation(word: str) -> str:
    return word.strip("\"'()[]{}.,!?:;")


def _candidate_identity_tokens(text: str) -> list[str]:
    """Every capitalized token in `text` that plausibly identifies a person.

    A capitalized word is exempt from grounding only when it is a
    closed-class function word (see `_CAPITALIZED_STOPWORDS`) capitalized
    purely because it opens a sentence or a Title-Cased heading. Every other
    capitalized token -- regardless of which verb or words surround it -- is
    a candidate identity that must be grounded. Applied to both the title and
    the overview: an ungrounded name is exactly as much of a cross-account
    leak in a title as it is in prose.
    """

    normalized = _ELLA_OVERVIEW_PREFIX_RE.sub("", text or "")
    candidates: list[str] = []
    for raw_word in normalized.split():
        word = _strip_word_punctuation(raw_word)
        match = _PROPER_NAME_TOKEN_RE.match(word)
        if not match:
            continue
        token = _strip_possessive_suffix(match.group(0))
        if token in _CAPITALIZED_STOPWORDS:
            continue
        if not _is_name_shaped_token(token):
            continue
        candidates.append(token)
    return candidates


def _account_profile_name_tokens(uid: str) -> set[str]:
    """Name tokens grounded in the authenticated account's own validated profile."""

    try:
        profile = get_user_from_uid(uid)
    except Exception:
        logger.exception("Failed to resolve account profile for correction identity gate", extra={"uid": uid})
        return set()
    if not isinstance(profile, dict):
        return set()
    display_name = profile.get("display_name")
    if not isinstance(display_name, str) or not display_name.strip():
        return set()
    return _extract_capitalized_tokens(display_name)


def _has_vocative_or_salutation(text: str) -> bool:
    return bool(
        _VOCATIVE_GREETING_RE.search(text)
        or _VOCATIVE_LEADING_ADDRESS_RE.search(text)
        or _VOCATIVE_TRAILING_ADDRESS_RE.search(text)
    )


def _correction_identity_gate_diagnostic(corrected: Optional[dict[str, Any]]) -> dict[str, str]:
    """Classify the existing rejection pattern without retaining generated text."""
    if not isinstance(corrected, dict):
        return {}
    fields = (
        ("title", str(corrected.get("title") or "")),
        ("overview", _ELLA_OVERVIEW_PREFIX_RE.sub("", str(corrected.get("overview") or ""))),
    )
    patterns = (
        ("greeting", _VOCATIVE_GREETING_RE),
        ("leading_address", _VOCATIVE_LEADING_ADDRESS_RE),
        ("trailing_address", _VOCATIVE_TRAILING_ADDRESS_RE),
    )
    for field, text in fields:
        for pattern, expression in patterns:
            if expression.search(text):
                return {"identity_gate_field": field, "identity_gate_pattern": pattern}
    return {}


def _correction_grounded_name_tokens(
    *,
    uid: str,
    transcript: str,
    structured: dict[str, Any],
    correction_text: str,
) -> set[str]:
    """Identity evidence the gate accepts.

    This mirrors exactly what the correction prompt itself is allowed to draw
    a name from (see `_build_direct_correction_prompt`): the transcript, the
    current summary, and the user's own correction text -- plus the bound
    account's own validated profile name. Accepting anything narrower would
    block legitimate corrections that resolve a name through the correction
    text or the current summary; accepting anything broader would defeat the
    gate. Matching is case-sensitive throughout: a lowercase occurrence (e.g.
    "will" as an auxiliary verb) never grounds a capitalized name ("Will").
    """

    grounded = set(_PERSONA_NAME_ALLOWLIST)
    grounded |= _extract_capitalized_tokens(transcript)
    grounded |= _extract_capitalized_tokens(correction_text)
    grounded |= _extract_capitalized_tokens(str(structured.get("title") or ""))
    grounded |= _extract_capitalized_tokens(str(structured.get("overview") or ""))
    grounded |= _account_profile_name_tokens(uid)
    return grounded


def _enforce_correction_identity_gate(
    *,
    uid: str,
    transcript: str,
    structured: dict[str, Any],
    correction_text: str,
    corrected: dict[str, Any],
) -> None:
    """Fail-closed guard applied before a corrected summary is written back.

    Rejects output that talks to the account holder directly (a vocative or
    salutation) or that introduces a capitalized proper name not grounded in
    this conversation's transcript, its current summary, the user's own
    correction text, or the authenticated account's own validated profile.

    Both the title and the overview are checked for ungrounded names: a short
    Title-Cased label is just as capable of carrying a leaked cross-account
    name as a sentence of prose.
    """

    title = str(corrected.get("title") or "")
    overview = str(corrected.get("overview") or "")
    overview_body = _ELLA_OVERVIEW_PREFIX_RE.sub("", overview)

    # Checked separately (rather than concatenated) so the "start of string"
    # anchors used to detect a leading salutation apply to each field's own
    # beginning, not to the start of a combined blob.
    if _has_vocative_or_salutation(title) or _has_vocative_or_salutation(overview_body):
        raise CorrectionIdentityGateError("vocative_or_salutation_detected")

    grounded = _correction_grounded_name_tokens(
        uid=uid,
        transcript=transcript,
        structured=structured,
        correction_text=correction_text,
    )
    candidates = _candidate_identity_tokens(title) + _candidate_identity_tokens(overview)
    for candidate in candidates:
        if candidate not in grounded:
            raise CorrectionIdentityGateError("ungrounded_name_detected")


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
    corrected_summary = None
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
        _enforce_correction_identity_gate(
            uid=uid,
            transcript=transcript,
            structured=structured,
            correction_text=request.correction_text,
            corrected=corrected_summary,
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
                "applied_at": applied_at,
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
    except CorrectionIdentityGateError as exc:
        # Fail closed: keep the prior summary version and report a generic,
        # non-leaky status rather than the rejected title/overview text.
        diagnostic = (
            _correction_identity_gate_diagnostic(corrected_summary)
            if exc.reason == "vocative_or_salutation_detected"
            else {}
        )
        logger.warning(
            "Direct conversation correction blocked by identity gate",
            extra={
                "uid": uid,
                "conversation_id": conversation_id,
                "correction_id": correction_id,
                "reason": exc.reason,
                **diagnostic,
            },
        )
        blocked_at = _now_iso()
        _append_correction_event(
            uid,
            conversation_id,
            correction_id,
            {
                "stage": "direct_apply_blocked_identity_gate",
                "status": "blocked",
                "at": blocked_at,
                "trace_id": trace_id,
                "reason": exc.reason,
                **diagnostic,
            },
        )
        _persist_correction_audit(
            uid,
            conversation_id,
            correction_id,
            {
                "status": "correction_blocked_identity_gate",
                "updated_at": blocked_at,
                "direct_apply_blocked_reason": exc.reason,
                **diagnostic,
            },
        )
        _update_conversation_correction_state(
            uid,
            conversation_id,
            {
                "correction_state": {
                    "status": "correction_blocked_identity_gate",
                    "pending": False,
                    "correction_id": correction_id,
                    "trace_id": trace_id,
                    "submitted_at": submitted_at,
                    "updated_at": blocked_at,
                }
            },
        )
        return ConversationCorrectionResponse(
            correction_id=correction_id,
            conversation_id=conversation_id,
            trace_id=trace_id,
            status="correction_blocked_identity_gate",
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
    uid: str = Depends(get_exact_firebase_uid),
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

    await _queue_correction_observer_work(
        background_tasks=background_tasks,
        uid=uid,
        conversation_id=conversation_id,
        correction_id=correction_id,
        trace_id=trace_id,
        request=request,
        source_conversation=conversation,
        structured=structured,
        submitted_at=submitted_at,
        active_summary_version_id=submitted_state.get("active_summary_version_id"),
        proposal_id=proposal_id,
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


@router.get(
    "/v1/conversations/{conversation_id}/processing-retry-plan",
    response_model=ConversationProcessingRetryPlan,
)
def get_conversation_processing_retry_plan(
    conversation_id: str,
    uid: str = Depends(get_exact_firebase_uid),
) -> ConversationProcessingRetryPlan:
    plan = build_conversation_processing_retry_plan(uid, conversation_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return ConversationProcessingRetryPlan(**plan)


@router.post(
    "/v1/conversations/{conversation_id}/processing-retries",
    response_model=RetryConversationProcessingResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_failed_conversation_processing(
    conversation_id: str,
    request: RetryConversationProcessingRequest,
    background_tasks: BackgroundTasks,
    uid: str = Depends(get_exact_firebase_uid),
) -> RetryConversationProcessingResponse:
    """Atomically queue generic recovery followed by canonical Hermes enrichment."""

    request_id = str(request.request_id)
    claim = conversations_db.claim_conversation_processing_retry(uid, conversation_id, request_id)
    outcome = claim["outcome"]
    if outcome == "not_found":
        raise HTTPException(status_code=404, detail="Conversation not found")
    if outcome == "locked":
        raise HTTPException(status_code=402, detail="Conversation locked")
    if outcome == "not_retryable":
        raise HTTPException(status_code=409, detail="Conversation processing cannot be retried safely")
    if outcome == "invalid_state":
        raise HTTPException(status_code=409, detail="Conversation is not in a retryable state")
    if outcome == "busy":
        raise HTTPException(status_code=409, detail="Another conversation processing retry is already active")

    conversation_data = conversations_db.get_conversation(uid, conversation_id)
    if conversation_data is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    try:
        conversation = Conversation(**conversation_data)
    except Exception as error:
        logger.exception(
            "Failed to decode claimed conversation processing retry",
            extra={"uid": uid, "conversation_id": conversation_id, "request_id": request_id},
        )
        if outcome == "claimed":
            conversations_db.finish_conversation_processing_retry(
                uid,
                conversation_id,
                request_id,
                ConversationStatus.failed.value,
                error_code="conversation_transcript_decode_failed",
                attempt_count=claim.get("attempt_count") or 1,
            )
        raise HTTPException(status_code=500, detail="Conversation could not be loaded safely") from error
    if outcome == "claimed":
        background_tasks.add_task(
            recover_failed_conversation_summary,
            uid=uid,
            conversation_id=conversation_id,
            request_id=request_id,
            client_context=request.correction_text,
            attempt_count=claim.get("attempt_count") or 1,
        )
        outcome = ConversationProcessingRetryOutcome.processing
    else:
        outcome = ConversationProcessingRetryOutcome(outcome)

    return RetryConversationProcessingResponse(
        outcome=outcome,
        recovery_mode=ConversationProcessingRecoveryMode(claim.get("mode") or "none"),
        phase=claim.get("phase"),
        generic_status=claim.get("generic_status"),
        generic_vector_status=claim.get("generic_vector_status"),
        enrichment_status=claim.get("enrichment_status"),
        vector_status=claim.get("vector_status"),
        lease_expires_at=claim.get("lease_expires_at"),
        attempt_count=claim.get("attempt_count") or 0,
        conversation=conversation,
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
    uid: str = Depends(require_current_ai_consent),
) -> ConversationCorrectionResponse:
    return await _submit_conversation_correction(conversation_id, request, background_tasks, uid)


@router.get(
    "/v1/ella/conversations/{conversation_id}/corrections/{correction_id}",
    response_model=ConversationCorrectionReceiptResponse,
)
def get_conversation_correction_receipt(
    conversation_id: str,
    correction_id: str,
    uid: str = Depends(get_exact_firebase_uid),
) -> ConversationCorrectionReceiptResponse:
    return _correction_receipt(
        uid=uid,
        conversation_id=conversation_id,
        correction_id=correction_id,
    )


@router.post(
    "/v1/ella/conversations/{conversation_id}/corrections/{correction_id}/undo",
    response_model=ConversationCorrectionReceiptResponse,
)
async def undo_conversation_correction(
    conversation_id: str,
    correction_id: str,
    uid: str = Depends(get_exact_firebase_uid),
) -> ConversationCorrectionReceiptResponse:
    conversation = conversations_db.get_conversation(uid, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    _require_unlocked_conversation(conversation)
    before, corrected = _correction_summary_versions(conversation, correction_id)
    if before is None or corrected is None:
        raise HTTPException(status_code=404, detail="Applied correction not found")

    audit_snapshot = _audit_ref(uid, conversation_id, correction_id).get()
    audit = audit_snapshot.to_dict() if getattr(audit_snapshot, "exists", False) else {}
    if audit.get("status") == "undone":
        return _correction_receipt(
            uid=uid,
            conversation_id=conversation_id,
            correction_id=correction_id,
            conversation=conversation,
        )
    corrected_version_id = str(corrected.get("id") or "")
    undo_version = _summary_version_by_kind_and_base(
        conversation,
        kind="correction_undo",
        based_on_version_id=corrected_version_id,
    )
    undo_version_id = str(undo_version.get("id") or "") if undo_version else ""
    active_version_id = str(conversation.get("active_summary_version_id") or "")
    source_already_reverted = bool(undo_version_id and active_version_id == undo_version_id)
    if active_version_id != corrected_version_id and not source_already_reverted:
        raise HTTPException(status_code=409, detail="A newer memory update must be undone first")

    rollback_plan = _prepare_applied_propagation_rollbacks(uid, conversation_id, correction_id)
    trace_id = f"correction-undo:{conversation_id}:{correction_id}"
    prepared_at = _now_iso()
    _persist_correction_audit(
        uid,
        conversation_id,
        correction_id,
        {
            "undo_operation": {
                "status": "prepared",
                "expected_source_version_id": corrected_version_id,
                "related_target_count": len(rollback_plan),
                "prepared_at": prepared_at,
            },
            "updated_at": prepared_at,
        },
    )
    reverted_count = await _revert_applied_propagations(
        uid,
        conversation_id,
        correction_id,
        rollback_plan=rollback_plan,
    )
    propagation_reverted_at = _now_iso()
    _persist_correction_audit(
        uid,
        conversation_id,
        correction_id,
        {
            "undo_operation": {
                "status": "propagation_reverted",
                "expected_source_version_id": corrected_version_id,
                "related_target_count": len(rollback_plan),
                "propagation_reverted_count": reverted_count,
                "updated_at": propagation_reverted_at,
            },
            "updated_at": propagation_reverted_at,
        },
    )
    if not source_already_reverted:
        try:
            apply_result = await apply_summary_update(
                uid=uid,
                conversation_id=conversation_id,
                trace_id=trace_id,
                active_summary_version_id=corrected_version_id,
                summary={
                    "title": before.get("title"),
                    "overview": before.get("overview"),
                    "emoji": before.get("emoji"),
                    "category": before.get("category"),
                },
                summary_kind="correction_undo",
                summary_source="ios",
                require_based_on_match=True,
                preserve_generated_results=True,
            )
        except ConcurrentConversationSummaryChangeError as exc:
            raise HTTPException(status_code=409, detail="A newer memory update must be undone first") from exc
        undo_version_id = str(apply_result.get("active_summary_version_id") or "")
        if not undo_version_id:
            raise HTTPException(status_code=500, detail="Correction undo version was not recorded")
    undone_at = _now_iso()
    _persist_correction_audit(
        uid,
        conversation_id,
        correction_id,
        {
            "status": "undone",
            "undone_at": undone_at,
            "updated_at": undone_at,
            "propagation_reverted_count": reverted_count,
            "undo_version_id": undo_version_id,
            "undo_operation": {
                "status": "completed",
                "expected_source_version_id": corrected_version_id,
                "undo_version_id": undo_version_id,
                "related_target_count": len(rollback_plan),
                "propagation_reverted_count": reverted_count,
                "completed_at": undone_at,
            },
        },
    )
    latest = conversations_db.get_conversation(uid, conversation_id) or conversation
    conversations_db.update_conversation(
        uid,
        conversation_id,
        {
            "correction_state": {
                "correction_id": correction_id,
                "status": "undone",
                "pending": False,
                "source": (conversation.get("correction_state") or {}).get("source"),
                "submitted_at": (conversation.get("correction_state") or {}).get("submitted_at"),
                "updated_at": datetime.now(timezone.utc),
                "active_summary_version_id": undo_version_id,
            }
        },
    )
    latest = conversations_db.get_conversation(uid, conversation_id) or latest
    return _correction_receipt(
        uid=uid,
        conversation_id=conversation_id,
        correction_id=correction_id,
        conversation=latest,
    )


@router.post(
    "/v1/conversations/{conversation_id}/corrections",
    response_model=ConversationCorrectionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_conversation_correction(
    conversation_id: str,
    request: ConversationCorrectionRequest,
    background_tasks: BackgroundTasks,
    uid: str = Depends(require_current_ai_consent),
) -> ConversationCorrectionResponse:
    return await _submit_conversation_correction(conversation_id, request, background_tasks, uid)
