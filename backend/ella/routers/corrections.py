"""
Ella conversation correction endpoints.

This router is intentionally registered through the Ella extension hook instead
of the upstream OMI conversation router so future upstream syncs do not have to
carry this custom app contract as a core patch.
"""

import asyncio
import hashlib
import json
import logging
import math
import os
import time
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from google.cloud.firestore_v1 import transactional
from pydantic import AliasChoices, BaseModel, Field, field_validator

import database.conversations as conversations_db
from database._client import db
from ella.config import ELLA_CONFIG
from ella.routers.canonical_events import CanonicalEventIn, PostgresCanonicalEventStore
from ella.services.ai_consent import require_current_ai_consent
from ella.services.correction_propagation import propagation_run_to_dict, run_correction_propagation
from ella.services.hermes_session import canonical_omi_session_key, safe_session_component
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
from ella.services.summary_writeback import (
    CanonicalSummaryWriteUnconfirmedError,
    ConcurrentConversationSummaryChangeError,
)
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
    os.getenv("HERMES_CHAT_COMPLETIONS_URL", "http://100.76.138.56:8642/v1/chat/completions"),
)
HERMES_CORRECTION_MODEL = os.getenv(
    "ELLA_CORRECTION_HERMES_MODEL",
    os.getenv("HERMES_CORRECTION_MODEL", "ella-plato-hermes-eval"),
)
DIRECT_CORRECTION_API_URL = os.getenv("ELLA_CORRECTION_API_URL", "https://api.x.ai/v1/chat/completions")
DIRECT_CORRECTION_MODEL = os.getenv("ELLA_CORRECTION_MODEL", "grok-4.3")
CORRECTION_PROVIDER_TIMEOUT_MAX_SECONDS = 120.0
CORRECTION_TERMINAL_OVERHEAD_SECONDS = 30.0
CORRECTION_TERMINAL_BOUND_SECONDS = CORRECTION_PROVIDER_TIMEOUT_MAX_SECONDS + CORRECTION_TERMINAL_OVERHEAD_SECONDS
CORRECTION_CLIENT_POLL_MARGIN_SECONDS = 30.0
CORRECTION_CLIENT_POLL_BUDGET_SECONDS = (2 * CORRECTION_TERMINAL_BOUND_SECONDS) + CORRECTION_CLIENT_POLL_MARGIN_SECONDS
CORRECTION_END_TO_END_DEADLINE_SECONDS = CORRECTION_TERMINAL_BOUND_SECONDS - 5.0


def _bounded_correction_provider_timeout(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 45.0
    if not math.isfinite(parsed):
        return 45.0
    return min(max(parsed, 1.0), CORRECTION_PROVIDER_TIMEOUT_MAX_SECONDS)


DIRECT_CORRECTION_TIMEOUT_SECONDS = _bounded_correction_provider_timeout(
    os.getenv("ELLA_CORRECTION_TIMEOUT_SECONDS", "45")
)
CORRECTION_RETRY_LEASE_SECONDS = CORRECTION_TERMINAL_BOUND_SECONDS
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


class _CorrectionDeadline:
    def __init__(self, *, started_at: Optional[float] = None, budget_seconds: Optional[float] = None):
        self.started_at = time.monotonic() if started_at is None else started_at
        self.budget_seconds = (
            CORRECTION_END_TO_END_DEADLINE_SECONDS if budget_seconds is None else max(0.0, budget_seconds)
        )

    def remaining(self) -> float:
        return max(0.0, self.budget_seconds - (time.monotonic() - self.started_at))

    def require_remaining(self) -> float:
        remaining = self.remaining()
        if remaining <= 0:
            raise TimeoutError("correction_deadline_exceeded")
        return remaining

    async def run_async(self, operation):
        return await asyncio.wait_for(operation(), timeout=self.require_remaining())

    async def run_blocking(self, operation, *args, **kwargs):
        return await asyncio.wait_for(
            asyncio.to_thread(operation, *args, **kwargs),
            timeout=self.require_remaining(),
        )

    async def run_cleanup_blocking(self, operation, *args, **kwargs):
        terminal_remaining = max(
            0.0,
            CORRECTION_TERMINAL_BOUND_SECONDS - (time.monotonic() - self.started_at),
        )
        if terminal_remaining <= 0:
            raise TimeoutError("correction_terminal_cleanup_deadline_exceeded")
        return await asyncio.wait_for(
            asyncio.to_thread(operation, *args, **kwargs),
            timeout=min(5.0, terminal_remaining),
        )


class SummaryContext(BaseModel):
    title: Optional[str] = None
    overview: Optional[str] = None
    app_summary: Optional[str] = None


class ConversationCorrectionRequest(BaseModel):
    correction_id: Optional[uuid.UUID] = None
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


def _correction_request_fingerprint(request: ConversationCorrectionRequest) -> str:
    payload = {
        "correction_text": request.correction_text,
        "source": request.source,
        "summary_context": request.summary_context.model_dump(),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


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
    failure_code: Optional[str] = None
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
    deadline: Optional[_CorrectionDeadline] = None,
) -> dict[str, Any]:
    deadline = deadline or _CorrectionDeadline()
    prompt = _build_direct_correction_prompt(
        request=request,
        structured=structured,
        transcript=transcript,
        segment_count=segment_count,
    )
    # Runtime resolution can itself cross a managed-provider boundary.
    await deadline.run_blocking(require_current_ai_consent, uid)
    config = await deadline.run_async(
        lambda: summary_provider_config_for_uid(
            uid,
            SummaryProviderConfig(
                provider=CORRECTION_PROVIDER,
                hermes_url=HERMES_CORRECTION_API_URL,
                hermes_model=HERMES_CORRECTION_MODEL,
                hermes_api_key=_hermes_correction_api_key(),
                legacy_url=DIRECT_CORRECTION_API_URL,
                legacy_model=DIRECT_CORRECTION_MODEL,
                legacy_api_key=_legacy_correction_api_key(),
                timeout_seconds=min(DIRECT_CORRECTION_TIMEOUT_SECONDS, deadline.require_remaining()),
            ),
        )
    )
    # Consent can be revoked while runtime authority is resolving. Revalidate
    # again at the last possible point before retained transcript egress.
    await deadline.run_blocking(require_current_ai_consent, uid)
    config = replace(config, timeout_seconds=min(config.timeout_seconds, deadline.require_remaining()))

    async def require_consent_before_provider_egress() -> None:
        await deadline.run_blocking(require_current_ai_consent, uid)

    return await deadline.run_async(
        lambda: generate_summary_from_prompt(
            prompt=prompt,
            fallback=structured,
            session_id=_correction_session_id(uid, conversation_id, correction_id),
            session_key=_correction_session_key(uid),
            trace_id=trace_id,
            required_tags=("omi", "correction"),
            config=config,
            async_client_factory=httpx.AsyncClient,
            egress_guard=require_consent_before_provider_egress,
        )
    )


def _apply_correction_source_in_transaction(
    transaction,
    conversation_ref,
    audit_ref,
    *,
    uid: str,
    conversation_id: str,
    correction_id: str,
    expected_active_summary_version_id: Optional[str],
    retry_attempt_token: str,
    update_data: dict[str, Any],
    transitioned_at: str,
) -> str:
    """Commit source CAS and canonical-pending ownership in one transaction."""

    conversation_snapshot = conversation_ref.get(transaction=transaction)
    audit_snapshot = audit_ref.get(transaction=transaction)
    if not getattr(conversation_snapshot, "exists", False) or not getattr(audit_snapshot, "exists", False):
        return "missing"
    conversation = conversation_snapshot.to_dict() or {}
    audit = audit_snapshot.to_dict() or {}
    if (
        str(audit.get("uid") or "") != uid
        or str(audit.get("conversation_id") or "") != conversation_id
        or str(audit.get("correction_id") or "") != correction_id
        or str(audit.get("retry_attempt_token") or "") != retry_attempt_token
        or str(audit.get("status") or "") not in {"queued", "retry_queued", "processing", "canonical_pending"}
    ):
        return "stale_attempt"
    if str(conversation.get("active_summary_version_id") or "") != str(expected_active_summary_version_id or ""):
        return "version_drift"
    lease_expires_at = _parse_correction_timestamp(audit.get("retry_lease_expires_at"))
    if lease_expires_at is not None and lease_expires_at <= datetime.now(timezone.utc):
        return "lease_expired"
    state = conversation.get("correction_state") or {}
    if state and str(state.get("correction_id") or "") != correction_id:
        return "stale_attempt"
    doc_level = conversation.get("data_protection_level", "standard")
    prepared_data = conversations_db._prepare_conversation_for_write(update_data, uid, doc_level)
    transaction.update(conversation_ref, prepared_data)
    transaction.set(
        audit_ref,
        {
            "status": "canonical_pending",
            "pending": True,
            "updated_at": transitioned_at,
            "failure_code": "canonical_write_unconfirmed",
            "retry_attempt_token": retry_attempt_token,
        },
        merge=True,
    )
    return "updated"


@transactional
def _apply_correction_source_transaction(transaction, conversation_ref, audit_ref, **kwargs) -> str:
    return _apply_correction_source_in_transaction(transaction, conversation_ref, audit_ref, **kwargs)


def _apply_correction_source_compare_and_set(
    *,
    uid: str,
    conversation_id: str,
    correction_id: str,
    expected_active_summary_version_id: Optional[str],
    retry_attempt_token: str,
    update_data: dict[str, Any],
) -> str:
    conversation_ref = db.collection("users").document(uid).collection("conversations").document(conversation_id)
    return _apply_correction_source_transaction(
        db.transaction(),
        conversation_ref,
        _audit_ref(uid, conversation_id, correction_id),
        uid=uid,
        conversation_id=conversation_id,
        correction_id=correction_id,
        expected_active_summary_version_id=expected_active_summary_version_id,
        retry_attempt_token=retry_attempt_token,
        update_data=update_data,
        transitioned_at=_now_iso(),
    )


async def _apply_corrected_summary(
    *,
    uid: str,
    conversation_id: str,
    correction_id: str,
    trace_id: str,
    active_summary_version_id: Optional[str],
    corrected: dict[str, Any],
    retry_attempt_token: Optional[str] = None,
) -> dict[str, Any]:
    return await apply_summary_update(
        uid=uid,
        conversation_id=conversation_id,
        trace_id=trace_id,
        active_summary_version_id=active_summary_version_id,
        summary=corrected,
        summary_kind="corrected_enriched",
        correction_id=correction_id,
        require_based_on_match=True,
        require_canonical=True,
        canonical_egress_guard=lambda: require_current_ai_consent(uid),
        correction_attempt_token=retry_attempt_token,
        correction_source_compare_and_set=(
            (
                lambda update_data: _apply_correction_source_compare_and_set(
                    uid=uid,
                    conversation_id=conversation_id,
                    correction_id=correction_id,
                    expected_active_summary_version_id=active_summary_version_id,
                    retry_attempt_token=retry_attempt_token,
                    update_data=update_data,
                )
            )
            if retry_attempt_token
            else None
        ),
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


def _read_correction_audit(uid: str, conversation_id: str, correction_id: str) -> dict[str, Any]:
    snapshot = _audit_ref(uid, conversation_id, correction_id).get()
    if not getattr(snapshot, "exists", False):
        return {}
    value = snapshot.to_dict() or {}
    return value if isinstance(value, dict) else {}


def _audit_request_fingerprint(audit: dict[str, Any]) -> str:
    recorded = str(audit.get("request_fingerprint") or "")
    if recorded:
        return recorded
    try:
        request = ConversationCorrectionRequest(
            correction_id=audit.get("correction_id"),
            correction_text=str(audit.get("correction_text") or ""),
            source=str(audit.get("source") or "ios"),
            summary_context=audit.get("summary_context") if isinstance(audit.get("summary_context"), dict) else {},
        )
    except Exception:
        return ""
    return _correction_request_fingerprint(request)


def _claim_initial_correction_submission_in_transaction(
    transaction,
    conversation_ref,
    audit_ref,
    *,
    uid: str,
    conversation_id: str,
    correction_id: str,
    expected_active_summary_version_id: str,
    bootstrap_update: dict[str, Any],
    correction_state: dict[str, Any],
    audit_payload: dict[str, Any],
) -> dict[str, Any]:
    """Atomically create the leased initial receipt or replay its exact request."""

    conversation_snapshot = conversation_ref.get(transaction=transaction)
    audit_snapshot = audit_ref.get(transaction=transaction)
    if not getattr(conversation_snapshot, "exists", False):
        return {"outcome": "conversation_missing"}
    if getattr(audit_snapshot, "exists", False):
        audit = audit_snapshot.to_dict() or {}
        if (
            str(audit.get("uid") or "") != uid
            or str(audit.get("conversation_id") or "") != conversation_id
            or str(audit.get("correction_id") or "") != correction_id
            or _audit_request_fingerprint(audit) != str(audit_payload.get("request_fingerprint") or "")
        ):
            return {"outcome": "idempotency_conflict"}
        return {"outcome": "replay", "audit": audit}

    conversation = conversation_snapshot.to_dict() or {}
    current_active_summary_version_id = str(
        conversation.get("active_summary_version_id") or bootstrap_update.get("active_summary_version_id") or ""
    )
    if current_active_summary_version_id != expected_active_summary_version_id:
        return {"outcome": "version_drift"}
    transaction.set(
        conversation_ref,
        {**bootstrap_update, "correction_state": correction_state},
        merge=True,
    )
    transaction.set(audit_ref, audit_payload)
    return {"outcome": "created", "audit": audit_payload}


@transactional
def _claim_initial_correction_submission_transaction(transaction, conversation_ref, audit_ref, **kwargs):
    return _claim_initial_correction_submission_in_transaction(
        transaction,
        conversation_ref,
        audit_ref,
        **kwargs,
    )


def _claim_initial_correction_submission(
    *,
    uid: str,
    conversation_id: str,
    correction_id: str,
    expected_active_summary_version_id: str,
    bootstrap_update: dict[str, Any],
    correction_state: dict[str, Any],
    audit_payload: dict[str, Any],
) -> dict[str, Any]:
    conversation_ref = db.collection("users").document(uid).collection("conversations").document(conversation_id)
    return _claim_initial_correction_submission_transaction(
        db.transaction(),
        conversation_ref,
        _audit_ref(uid, conversation_id, correction_id),
        uid=uid,
        conversation_id=conversation_id,
        correction_id=correction_id,
        expected_active_summary_version_id=expected_active_summary_version_id,
        bootstrap_update=bootstrap_update,
        correction_state=correction_state,
        audit_payload=audit_payload,
    )


def _correction_failure_code(error: Exception) -> str:
    candidate = str(getattr(error, "code", "") or "").strip()
    if candidate and all(character.isalnum() or character in {"_", "-"} for character in candidate):
        return candidate[:128]
    return error.__class__.__name__.lower()[:128]


def _audit_has_completed_stage(audit: dict[str, Any], *stages: str) -> bool:
    completed = set(stages)
    return any(
        isinstance(event, dict) and event.get("stage") in completed and event.get("status") in {"ok", "completed"}
        for event in audit.get("events") or []
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


def _correction_response_from_audit(
    *,
    conversation_id: str,
    correction_id: str,
    audit: dict[str, Any],
) -> ConversationCorrectionResponse:
    status_value = str(audit.get("status") or "queued")
    return ConversationCorrectionResponse(
        correction_id=correction_id,
        conversation_id=conversation_id,
        trace_id=str(audit.get("trace_id") or f"correction:{conversation_id}:{correction_id}"),
        status=status_value,
        queued=status_value in {"submitted", "queued", "processing", "retry_queued", "canonical_pending"},
        proposal_id=str(audit.get("proposal_id") or "") or None,
    )


def _finalize_revoked_correction_in_transaction(
    transaction,
    conversation_ref,
    audit_ref,
    *,
    uid: str,
    conversation_id: str,
    correction_id: str,
    expected_retry_attempt_token: Optional[str],
    finalized_at: str,
) -> dict[str, Any]:
    """Fence a revoked attempt and preserve any already-committed source correction."""

    conversation_snapshot = conversation_ref.get(transaction=transaction)
    audit_snapshot = audit_ref.get(transaction=transaction)
    if not getattr(conversation_snapshot, "exists", False) or not getattr(audit_snapshot, "exists", False):
        return {"outcome": "missing"}
    conversation = conversation_snapshot.to_dict() or {}
    audit = audit_snapshot.to_dict() or {}
    if (
        str(audit.get("uid") or "") != uid
        or str(audit.get("conversation_id") or "") != conversation_id
        or str(audit.get("correction_id") or "") != correction_id
    ):
        return {"outcome": "identity_mismatch"}
    if expected_retry_attempt_token and str(audit.get("retry_attempt_token") or "") != expected_retry_attempt_token:
        return {"outcome": "stale_attempt", "audit": audit}
    if str(audit.get("status") or "") in {"applied", "undone", "consent_revoked"}:
        return {"outcome": "already_terminal", "audit": audit}

    _, corrected = _correction_summary_versions(conversation, correction_id)
    corrected_id = str(corrected.get("id") or "") if corrected else ""
    source_committed = bool(corrected_id and str(conversation.get("active_summary_version_id") or "") == corrected_id)
    status_value = "applied" if source_committed else "consent_revoked"
    failure_code = "consent_revoked_after_source_apply" if source_committed else "ai_consent_required"
    audit_update = {
        "status": status_value,
        "pending": False,
        "updated_at": finalized_at,
        "failure_code": failure_code,
        "retry_lease_expires_at": None,
    }
    if source_committed:
        audit_update["applied_at"] = audit.get("applied_at") or finalized_at
    transaction.set(audit_ref, audit_update, merge=True)

    state = conversation.get("correction_state") or {}
    if str(state.get("correction_id") or "") == correction_id:
        transaction.set(
            conversation_ref,
            {
                "correction_state": {
                    **state,
                    "status": status_value,
                    "pending": False,
                    "updated_at": finalized_at,
                    "failure_code": failure_code,
                    "active_summary_version_id": corrected_id or state.get("active_summary_version_id"),
                }
            },
            merge=True,
        )
    return {"outcome": "finalized", "audit": {**audit, **audit_update}}


@transactional
def _finalize_revoked_correction_transaction(transaction, conversation_ref, audit_ref, **kwargs):
    return _finalize_revoked_correction_in_transaction(transaction, conversation_ref, audit_ref, **kwargs)


def _finalize_revoked_correction(
    *,
    uid: str,
    conversation_id: str,
    correction_id: str,
    expected_retry_attempt_token: Optional[str] = None,
) -> dict[str, Any]:
    conversation_ref = db.collection("users").document(uid).collection("conversations").document(conversation_id)
    return _finalize_revoked_correction_transaction(
        db.transaction(),
        conversation_ref,
        _audit_ref(uid, conversation_id, correction_id),
        uid=uid,
        conversation_id=conversation_id,
        correction_id=correction_id,
        expected_retry_attempt_token=expected_retry_attempt_token,
        finalized_at=_now_iso(),
    )


def _require_correction_consent_or_terminal_response(
    *,
    uid: str,
    conversation_id: str,
    correction_id: str,
    expected_retry_attempt_token: Optional[str] = None,
) -> Optional[ConversationCorrectionResponse]:
    try:
        require_current_ai_consent(uid)
        return None
    except HTTPException as error:
        detail = error.detail if isinstance(error.detail, dict) else {}
        if error.status_code != 403 or detail.get("code") != "ai_consent_required":
            raise
    result = _finalize_revoked_correction(
        uid=uid,
        conversation_id=conversation_id,
        correction_id=correction_id,
        expected_retry_attempt_token=expected_retry_attempt_token,
    )
    audit = result.get("audit") if isinstance(result.get("audit"), dict) else {}
    if result.get("outcome") == "missing":
        raise HTTPException(status_code=404, detail="Correction not found")
    if result.get("outcome") == "identity_mismatch":
        raise HTTPException(status_code=404, detail="Correction not found")
    if result.get("outcome") == "stale_attempt":
        return _correction_response_from_audit(
            conversation_id=conversation_id,
            correction_id=correction_id,
            audit=audit,
        )
    return _correction_response_from_audit(
        conversation_id=conversation_id,
        correction_id=correction_id,
        audit=audit,
    )


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
    audit = _read_correction_audit(uid, conversation_id, correction_id)
    before, corrected = _correction_summary_versions(conversation, correction_id)
    if not audit and corrected is None:
        raise HTTPException(status_code=404, detail="Correction not found")
    raw_state = conversation.get("correction_state")
    state = (
        raw_state if isinstance(raw_state, dict) and str(raw_state.get("correction_id") or "") == correction_id else {}
    )
    status_value = str(audit.get("status") or state.get("status") or "pending")
    corrected_id = str(corrected.get("id") or "") if corrected else ""
    enrichment_state = conversation.get("enrichment_state") or {}
    expected_trace_id = str(audit.get("trace_id") or f"correction:{conversation_id}:{correction_id}")
    corrected_is_active = bool(
        corrected_id and str(conversation.get("active_summary_version_id") or "") == corrected_id
    )
    same_canonical_trace = str(enrichment_state.get("trace_id") or "") == expected_trace_id
    canonical_confirmed = bool(
        corrected_is_active
        and same_canonical_trace
        and enrichment_state.get("canonical_status") == "completed"
        and enrichment_state.get("status") == "writeback_applied"
    )
    canonical_pending = bool(
        corrected_is_active
        and same_canonical_trace
        and not canonical_confirmed
        and enrichment_state.get("status") == "writeback_pending_canonical"
    )
    if canonical_confirmed:
        status_value = "applied"
    elif canonical_pending and status_value != "applied":
        status_value = "canonical_pending"
    recorded_base_version_id = str(
        audit.get("active_summary_version_id") or state.get("active_summary_version_id") or ""
    )
    if before is None and recorded_base_version_id:
        before = _summary_version(conversation, recorded_base_version_id)
    applied_count, reverted_count, propagation_status = _correction_propagation_counts(
        uid,
        conversation_id,
        correction_id,
    )
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
        applied_at=audit.get("applied_at") or (enrichment_state.get("updated_at") if canonical_confirmed else None),
        undone_at=audit.get("undone_at"),
        before_version_id=(str(before.get("id") or "") or None) if before else None,
        after_version_id=corrected_id or None,
        active_version_id=str(conversation.get("active_summary_version_id") or "") or None,
        undo_version_id=(str(undo_version.get("id") or "") or None) if undo_version else None,
        failure_code=str(audit.get("failure_code") or state.get("failure_code") or "") or None,
        before=_snapshot(before),
        after=_snapshot(corrected),
        propagation_status=propagation_status,
        propagation_applied_count=applied_count,
        propagation_reverted_count=reverted_count,
    )


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
        _persist_correction_audit(
            uid,
            conversation_id,
            correction_id,
            {"canonical_event_completed": True, "updated_at": _now_iso()},
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
                "propagation_completed": True,
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
    audit = _read_correction_audit(uid, conversation_id, correction_id)
    canonical_completed = audit.get("canonical_event_completed") is True or _audit_has_completed_stage(
        audit, "canonical_event_emitted"
    )
    if not canonical_completed:
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

    audit = _read_correction_audit(uid, conversation_id, correction_id)
    propagation_completed = audit.get("propagation_completed") is True or _audit_has_completed_stage(
        audit, "propagation_run_completed"
    )
    if not propagation_completed:
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


async def _run_post_source_correction_side_effects(
    *,
    uid: str,
    conversation_id: str,
    correction_id: str,
    trace_id: str,
    request: ConversationCorrectionRequest,
    source_conversation: dict[str, Any],
    structured: dict[str, Any],
    transcript: str,
    segment_count: int,
    submitted_at: str,
    active_summary_version_id: Optional[str],
) -> Optional[str]:
    """Run only incomplete downstream work after the source summary CAS succeeds."""

    require_current_ai_consent(uid)
    audit = _read_correction_audit(uid, conversation_id, correction_id)
    proposal_id = str(audit.get("proposal_id") or "") or None
    proposal_completed = bool(proposal_id) or _audit_has_completed_stage(audit, "proposal_created")
    if not proposal_completed:
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
                active_summary_version_id=active_summary_version_id,
            )
            if proposal_id:
                created_at = _now_iso()
                _persist_correction_audit(
                    uid,
                    conversation_id,
                    correction_id,
                    {"proposal_id": proposal_id, "proposal_completed": True, "updated_at": created_at},
                )
                _append_correction_event(
                    uid,
                    conversation_id,
                    correction_id,
                    {
                        "stage": "proposal_created",
                        "status": "ok",
                        "at": created_at,
                        "trace_id": trace_id,
                        "proposal_id": proposal_id,
                    },
                )
        except Exception:
            logger.exception(
                "Failed to create summary correction proposal",
                extra={"uid": uid, "conversation_id": conversation_id, "correction_id": correction_id},
            )
            _append_correction_event(
                uid,
                conversation_id,
                correction_id,
                {"stage": "proposal_failed", "status": "error", "at": _now_iso(), "trace_id": trace_id},
            )

    require_current_ai_consent(uid)
    await _run_correction_observer_work(
        uid=uid,
        conversation_id=conversation_id,
        correction_id=correction_id,
        trace_id=trace_id,
        request=request,
        source_conversation=source_conversation,
        structured=structured,
        submitted_at=submitted_at,
        active_summary_version_id=active_summary_version_id,
        proposal_id=proposal_id,
    )
    return proposal_id


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
    require_current_ai_consent(uid)
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
    proposal_id: Optional[str] = None,
    source_conversation: Optional[dict[str, Any]] = None,
    retry_attempt_token: Optional[str] = None,
    retry_deadline_at: Optional[str] = None,
    deadline: Optional[_CorrectionDeadline] = None,
) -> ConversationCorrectionResponse:
    deadline = deadline or _CorrectionDeadline()
    if retry_attempt_token:
        attempt_status = await deadline.run_blocking(
            _start_correction_retry_attempt,
            uid=uid,
            conversation_id=conversation_id,
            correction_id=correction_id,
            recorded_base_version_id=str(active_summary_version_id or ""),
            retry_attempt_token=retry_attempt_token,
        )
        if attempt_status != "started":
            return _correction_response_from_audit(
                conversation_id=conversation_id,
                correction_id=correction_id,
                audit=await deadline.run_blocking(_read_correction_audit, uid, conversation_id, correction_id),
            )
    consent_terminal = await deadline.run_blocking(
        _require_correction_consent_or_terminal_response,
        uid=uid,
        conversation_id=conversation_id,
        correction_id=correction_id,
        expected_retry_attempt_token=retry_attempt_token,
    )
    if consent_terminal is not None:
        return consent_terminal
    parsed_retry_deadline = _parse_correction_timestamp(retry_deadline_at)
    if parsed_retry_deadline is not None:
        deadline.budget_seconds = min(
            deadline.budget_seconds,
            max(0.0, (parsed_retry_deadline - datetime.now(timezone.utc)).total_seconds())
            + (time.monotonic() - deadline.started_at),
        )
    try:
        corrected_summary = await deadline.run_async(
            lambda: _generate_corrected_summary(
                uid=uid,
                conversation_id=conversation_id,
                correction_id=correction_id,
                trace_id=trace_id,
                request=request,
                structured=structured,
                transcript=transcript,
                segment_count=segment_count,
                deadline=deadline,
            )
        )
        consent_terminal = await deadline.run_blocking(
            _require_correction_consent_or_terminal_response,
            uid=uid,
            conversation_id=conversation_id,
            correction_id=correction_id,
            expected_retry_attempt_token=retry_attempt_token,
        )
        if consent_terminal is not None:
            return consent_terminal
        if retry_attempt_token:
            attempt_status = await deadline.run_blocking(
                _start_correction_retry_attempt,
                uid=uid,
                conversation_id=conversation_id,
                correction_id=correction_id,
                recorded_base_version_id=str(active_summary_version_id or ""),
                retry_attempt_token=retry_attempt_token,
            )
            if attempt_status != "started":
                return _correction_response_from_audit(
                    conversation_id=conversation_id,
                    correction_id=correction_id,
                    audit=await deadline.run_blocking(_read_correction_audit, uid, conversation_id, correction_id),
                )
        apply_result = await deadline.run_async(
            lambda: _apply_corrected_summary(
                uid=uid,
                conversation_id=conversation_id,
                correction_id=correction_id,
                trace_id=trace_id,
                active_summary_version_id=active_summary_version_id,
                corrected=corrected_summary,
                retry_attempt_token=retry_attempt_token,
            )
        )
        if apply_result.get("canonical_confirmed") is not True:
            raise CanonicalSummaryWriteUnconfirmedError("canonical_write_unconfirmed")
        applied_at = _now_iso()
        finish_status = await deadline.run_blocking(
            _finish_canonical_reconciliation,
            uid=uid,
            conversation_id=conversation_id,
            correction_id=correction_id,
            recorded_base_version_id=str(active_summary_version_id or ""),
            retry_attempt_token=str(retry_attempt_token or ""),
            audit_update={
                "status": "applied",
                "pending": False,
                "applied_at": applied_at,
                "updated_at": applied_at,
                "direct_apply_result": apply_result,
                "direct_apply_summary": corrected_summary,
                "failure_code": None,
                "retry_lease_expires_at": None,
            },
        )
        if finish_status != "finished":
            return _correction_response_from_audit(
                conversation_id=conversation_id,
                correction_id=correction_id,
                audit=await deadline.run_blocking(_read_correction_audit, uid, conversation_id, correction_id),
            )
        try:
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
        except Exception:
            logger.exception(
                "Canonical correction applied before success event confirmation",
                extra={"uid": uid, "conversation_id": conversation_id, "correction_id": correction_id},
            )
        try:
            await deadline.run_blocking(require_current_ai_consent, uid)
            latest_source = (
                await deadline.run_blocking(conversations_db.get_conversation, uid, conversation_id)
                or source_conversation
                or {}
            )
            proposal_id = await deadline.run_async(
                lambda: _run_post_source_correction_side_effects(
                    uid=uid,
                    conversation_id=conversation_id,
                    correction_id=correction_id,
                    trace_id=trace_id,
                    request=request,
                    source_conversation=latest_source,
                    structured=structured,
                    transcript=transcript,
                    segment_count=segment_count,
                    submitted_at=submitted_at,
                    active_summary_version_id=active_summary_version_id,
                )
            )
        except Exception:
            logger.exception(
                "Post-source correction side effects failed",
                extra={"uid": uid, "conversation_id": conversation_id, "correction_id": correction_id},
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
        if isinstance(exc, HTTPException):
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            if exc.status_code == 403 and detail.get("code") == "ai_consent_required":
                terminal = await deadline.run_cleanup_blocking(
                    _require_correction_consent_or_terminal_response,
                    uid=uid,
                    conversation_id=conversation_id,
                    correction_id=correction_id,
                    expected_retry_attempt_token=retry_attempt_token,
                )
                if terminal is not None:
                    return terminal
        reconciled = await deadline.run_cleanup_blocking(
            _reconcile_committed_correction,
            uid=uid,
            conversation_id=conversation_id,
            correction_id=correction_id,
            trace_id=trace_id,
            active_summary_version_id=active_summary_version_id,
            proposal_id=proposal_id,
            expected_retry_attempt_token=retry_attempt_token,
        )
        if reconciled is not None:
            return reconciled
        if retry_attempt_token:
            attempt_status = await deadline.run_cleanup_blocking(
                _start_correction_retry_attempt,
                uid=uid,
                conversation_id=conversation_id,
                correction_id=correction_id,
                recorded_base_version_id=str(active_summary_version_id or ""),
                retry_attempt_token=retry_attempt_token,
            )
            if attempt_status in {"missing", "stale_attempt", "version_drift"}:
                return _correction_response_from_audit(
                    conversation_id=conversation_id,
                    correction_id=correction_id,
                    audit=await deadline.run_cleanup_blocking(
                        _read_correction_audit, uid, conversation_id, correction_id
                    ),
                )
        if isinstance(exc, CanonicalSummaryWriteUnconfirmedError):
            pending_at = _now_iso()
            try:
                finish_status = await deadline.run_cleanup_blocking(
                    _finish_canonical_reconciliation,
                    uid=uid,
                    conversation_id=conversation_id,
                    correction_id=correction_id,
                    recorded_base_version_id=str(active_summary_version_id or ""),
                    retry_attempt_token=str(retry_attempt_token or ""),
                    audit_update={
                        "status": "canonical_pending",
                        "pending": True,
                        "updated_at": pending_at,
                        "failure_code": "canonical_write_unconfirmed",
                        "source": request.source,
                        "retry_lease_expires_at": None,
                    },
                )
                if finish_status != "finished":
                    return _correction_response_from_audit(
                        conversation_id=conversation_id,
                        correction_id=correction_id,
                        audit=await deadline.run_cleanup_blocking(
                            _read_correction_audit, uid, conversation_id, correction_id
                        ),
                    )
                _append_correction_event(
                    uid,
                    conversation_id,
                    correction_id,
                    {
                        "stage": "canonical_pending",
                        "status": "pending",
                        "at": pending_at,
                        "trace_id": trace_id,
                        "failure_code": "canonical_write_unconfirmed",
                    },
                )
            except Exception:
                logger.exception(
                    "Failed to persist canonical-pending correction receipt",
                    extra={"uid": uid, "conversation_id": conversation_id, "correction_id": correction_id},
                )
            return ConversationCorrectionResponse(
                correction_id=correction_id,
                conversation_id=conversation_id,
                trace_id=trace_id,
                status="canonical_pending",
                queued=True,
                proposal_id=proposal_id,
            )
        logger.exception(
            "Direct conversation correction apply failed",
            extra={"uid": uid, "conversation_id": conversation_id, "correction_id": correction_id},
        )
        failed_at = _now_iso()
        failure_code = _correction_failure_code(exc)
        if not N8N_CORRECTION_FALLBACK_ENABLED:
            failed_audit = {
                "status": "direct_apply_failed",
                "updated_at": failed_at,
                "failure_code": failure_code,
                "source": request.source,
                "active_summary_version_id": active_summary_version_id,
                "retry_attempt_token": retry_attempt_token,
                "retry_lease_expires_at": None,
            }
            failed_state = {
                "status": "direct_apply_failed",
                "pending": False,
                "correction_id": correction_id,
                "trace_id": trace_id,
                "submitted_at": submitted_at,
                "updated_at": failed_at,
                "source": request.source,
                "active_summary_version_id": active_summary_version_id,
                "failure_code": failure_code,
            }
            failure_outcome = await deadline.run_cleanup_blocking(
                _record_failed_correction_attempt,
                uid=uid,
                conversation_id=conversation_id,
                correction_id=correction_id,
                retry_attempt_token=retry_attempt_token,
                audit_update=failed_audit,
                correction_state=failed_state,
            )
            if failure_outcome == "irreversible_success":
                reconciled = await deadline.run_cleanup_blocking(
                    _reconcile_committed_correction,
                    uid=uid,
                    conversation_id=conversation_id,
                    correction_id=correction_id,
                    trace_id=trace_id,
                    active_summary_version_id=active_summary_version_id,
                    proposal_id=proposal_id,
                    expected_retry_attempt_token=retry_attempt_token,
                )
                if reconciled is not None:
                    return reconciled
            if failure_outcome != "recorded":
                return _correction_response_from_audit(
                    conversation_id=conversation_id,
                    correction_id=correction_id,
                    audit=await deadline.run_cleanup_blocking(
                        _read_correction_audit, uid, conversation_id, correction_id
                    ),
                )
            _append_correction_event(
                uid,
                conversation_id,
                correction_id,
                {
                    "stage": "direct_apply_failed",
                    "status": "error",
                    "at": failed_at,
                    "trace_id": trace_id,
                    "failure_code": failure_code,
                    "n8n_fallback_enabled": False,
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
    deadline = _CorrectionDeadline()
    await deadline.run_blocking(require_current_ai_consent, uid)
    conversation = await deadline.run_blocking(conversations_db.get_conversation, uid, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if conversation.get("is_locked", False):
        raise HTTPException(status_code=402, detail="Conversation locked")

    correction_id = str(request.correction_id or uuid.uuid4())
    trace_id = f"correction:{conversation_id}:{correction_id}"
    submitted_at = _now_iso()
    structured = _structured_summary(conversation)
    transcript = _format_transcript(conversation)
    segment_count = len(conversation.get("transcript_segments") or [])
    bootstrap_update = {}
    bootstrap_builder = getattr(conversations_db, "bootstrap_summary_versioning_update", None)
    if callable(bootstrap_builder):
        bootstrap_update = bootstrap_builder(conversation)

    expected_active_summary_version_id = str(
        bootstrap_update.get("active_summary_version_id") or conversation.get("active_summary_version_id") or ""
    )
    initial_attempt_token = _new_correction_attempt_token() if DIRECT_CORRECTION_APPLY_ENABLED else None
    initial_lease_expires_at = (
        _correction_retry_lease_expiry(datetime.now(timezone.utc)) if initial_attempt_token else None
    )
    initial_queue_mode = (
        "background_direct_apply"
        if initial_attempt_token and DIRECT_CORRECTION_BACKGROUND_ENABLED and background_tasks is not None
        else "direct_apply" if initial_attempt_token else "fallback"
    )
    submitted_state = {
        "correction_id": correction_id,
        "trace_id": trace_id,
        "status": "queued",
        "pending": True,
        "source": request.source,
        "submitted_at": submitted_at,
        "updated_at": submitted_at,
        "active_summary_version_id": expected_active_summary_version_id,
        "retry_attempt_token": initial_attempt_token,
        "retry_lease_expires_at": initial_lease_expires_at,
    }

    audit_payload = {
        "correction_id": correction_id,
        "trace_id": trace_id,
        "uid": uid,
        "conversation_id": conversation_id,
        "status": "queued",
        "pending": True,
        "source": request.source,
        "correction_text": request.correction_text,
        "category": _correction_category(request.correction_text, request.summary_context),
        "summary_context": request.summary_context.model_dump(),
        "current_summary": structured,
        "segment_count": segment_count,
        "active_summary_version_id": expected_active_summary_version_id,
        "request_fingerprint": _correction_request_fingerprint(request),
        "queue_result": {"mode": initial_queue_mode},
        "retry_attempt_token": initial_attempt_token,
        "retry_lease_expires_at": initial_lease_expires_at,
        "created_at": submitted_at,
        "updated_at": submitted_at,
        "events": [{"stage": "queued", "status": "ok", "at": submitted_at, "trace_id": trace_id}],
    }
    initial_claim = await deadline.run_blocking(
        _claim_initial_correction_submission,
        uid=uid,
        conversation_id=conversation_id,
        correction_id=correction_id,
        expected_active_summary_version_id=expected_active_summary_version_id,
        bootstrap_update=bootstrap_update,
        correction_state=submitted_state,
        audit_payload=audit_payload,
    )
    claim_outcome = str(initial_claim.get("outcome") or "")
    if claim_outcome == "replay":
        return _correction_response_from_audit(
            conversation_id=conversation_id,
            correction_id=correction_id,
            audit=initial_claim.get("audit") or {},
        )
    if claim_outcome == "conversation_missing":
        raise HTTPException(status_code=404, detail="Conversation not found")
    if claim_outcome in {"version_drift", "idempotency_conflict"}:
        raise HTTPException(status_code=409, detail="Correction idempotency conflict")
    if claim_outcome != "created":
        raise HTTPException(status_code=500, detail="Correction submission claim failed")

    proposal_id = None

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
            "source_conversation": conversation,
            "retry_attempt_token": initial_attempt_token,
            "retry_deadline_at": initial_lease_expires_at,
            "deadline": deadline,
        }
        if DIRECT_CORRECTION_BACKGROUND_ENABLED and background_tasks is not None:
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
                "failure_code": "direct_apply_disabled",
                "source": request.source,
                "active_summary_version_id": submitted_state.get("active_summary_version_id"),
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
                    "source": request.source,
                    "active_summary_version_id": submitted_state.get("active_summary_version_id"),
                    "failure_code": "direct_apply_disabled",
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
                "failure_code": _correction_failure_code(exc),
                "source": request.source,
                "active_summary_version_id": submitted_state.get("active_summary_version_id"),
            },
        )
        _append_correction_event(
            uid,
            conversation_id,
            correction_id,
            {
                "stage": "queue_failed",
                "status": "error",
                "at": failed_at,
                "trace_id": trace_id,
                "failure_code": _correction_failure_code(exc),
            },
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
                    "failure_code": _correction_failure_code(exc),
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


def _recorded_correction_base_version_id(
    *,
    uid: str,
    conversation_id: str,
    correction_id: str,
    audit: dict[str, Any],
) -> str:
    recorded = str(audit.get("active_summary_version_id") or "")
    if recorded:
        return recorded

    proposal_id = str(audit.get("proposal_id") or "")
    if not proposal_id:
        return ""
    proposal = proposal_ingest.proposals_db.get_proposal(uid, proposal_id)
    if proposal is None:
        return ""
    target = proposal.payload.get("target") if isinstance(proposal.payload, dict) else None
    expected_idempotency_key = f"summary-correction:{uid}:{conversation_id}:{correction_id}"
    if (
        proposal.profile_uid != uid
        or proposal.idempotency_key != expected_idempotency_key
        or proposal.trace_id != str(audit.get("trace_id") or "")
        or not isinstance(target, dict)
        or str(target.get("conversation_id") or "") != conversation_id
        or str(target.get("correction_id") or "") != correction_id
    ):
        return ""
    return str(target.get("active_summary_version_id") or "")


def _exact_correction_retry_context(
    *,
    uid: str,
    conversation_id: str,
    correction_id: str,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    conversation = conversations_db.get_conversation(uid, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    _require_unlocked_conversation(conversation)
    audit = _read_correction_audit(uid, conversation_id, correction_id)
    if not audit:
        raise HTTPException(status_code=404, detail="Correction not found")
    if (
        str(audit.get("uid") or "") != uid
        or str(audit.get("conversation_id") or "") != conversation_id
        or str(audit.get("correction_id") or "") != correction_id
    ):
        raise HTTPException(status_code=404, detail="Correction not found")
    recorded_base_version_id = _recorded_correction_base_version_id(
        uid=uid,
        conversation_id=conversation_id,
        correction_id=correction_id,
        audit=audit,
    )
    if not recorded_base_version_id:
        raise HTTPException(status_code=409, detail="Correction base version is unavailable")
    return conversation, audit, recorded_base_version_id


def _parse_correction_timestamp(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _new_correction_attempt_token() -> str:
    return str(uuid.uuid4())


def _correction_retry_lease_expiry(now: datetime) -> str:
    return (now + timedelta(seconds=CORRECTION_RETRY_LEASE_SECONDS)).isoformat()


def _correction_work_lease_is_reclaimable(audit: dict[str, Any], *, now: datetime) -> bool:
    audit_status = str(audit.get("status") or "")
    if audit_status not in {
        "submitted",
        "queued",
        "processing",
        "retry_queued",
        "canonical_pending",
    }:
        return False
    if audit_status in {"queued", "processing", "retry_queued"} and not audit.get("retry_attempt_token"):
        return False
    lease_expires_at = _parse_correction_timestamp(audit.get("retry_lease_expires_at"))
    return lease_expires_at is None or lease_expires_at <= now.astimezone(timezone.utc)


def _claim_failed_correction_retry_in_transaction(
    transaction,
    conversation_ref,
    audit_ref,
    *,
    uid: str,
    conversation_id: str,
    correction_id: str,
    recorded_base_version_id: str,
    retry_queued_at: str,
    retry_lease_expires_at: str,
    retry_attempt_token: str,
    source: str,
) -> str:
    """Atomically fence version drift and duplicate exact-correction retry claims."""

    conversation_snapshot = conversation_ref.get(transaction=transaction)
    audit_snapshot = audit_ref.get(transaction=transaction)
    if not getattr(conversation_snapshot, "exists", False) or not getattr(audit_snapshot, "exists", False):
        return "missing"
    conversation = conversation_snapshot.to_dict() or {}
    audit = audit_snapshot.to_dict() or {}
    if str(conversation.get("active_summary_version_id") or "") != recorded_base_version_id:
        return "version_drift"
    audit_base_version_id = str(audit.get("active_summary_version_id") or "")
    if (
        str(audit.get("uid") or "") != uid
        or str(audit.get("conversation_id") or "") != conversation_id
        or str(audit.get("correction_id") or "") != correction_id
        or (audit_base_version_id and audit_base_version_id != recorded_base_version_id)
    ):
        return "identity_mismatch"
    audit_status = str(audit.get("status") or "")
    if audit_status in {"submitted", "queued", "processing", "retry_queued", "canonical_pending"}:
        lease_expires_at = _parse_correction_timestamp(audit.get("retry_lease_expires_at"))
        retry_now = _parse_correction_timestamp(retry_queued_at)
        if lease_expires_at is not None and retry_now is not None and lease_expires_at > retry_now:
            return "already_queued"
    elif audit_status not in {"direct_apply_failed", "direct_apply_disabled", "queue_failed"}:
        return "not_retryable"
    transaction.set(
        audit_ref,
        {
            "status": "retry_queued",
            "source": source,
            "active_summary_version_id": recorded_base_version_id,
            "retry_count": int(audit.get("retry_count") or 0) + 1,
            "retry_attempt_token": retry_attempt_token,
            "retry_lease_expires_at": retry_lease_expires_at,
            "updated_at": retry_queued_at,
        },
        merge=True,
    )
    return "claimed"


@transactional
def _claim_failed_correction_retry_transaction(
    transaction,
    conversation_ref,
    audit_ref,
    **kwargs,
) -> str:
    return _claim_failed_correction_retry_in_transaction(
        transaction,
        conversation_ref,
        audit_ref,
        **kwargs,
    )


def _claim_failed_correction_retry(
    *,
    uid: str,
    conversation_id: str,
    correction_id: str,
    recorded_base_version_id: str,
    retry_queued_at: str,
    retry_lease_expires_at: str,
    retry_attempt_token: str,
    source: str,
) -> str:
    conversation_ref = db.collection("users").document(uid).collection("conversations").document(conversation_id)
    return _claim_failed_correction_retry_transaction(
        db.transaction(),
        conversation_ref,
        _audit_ref(uid, conversation_id, correction_id),
        uid=uid,
        conversation_id=conversation_id,
        correction_id=correction_id,
        recorded_base_version_id=recorded_base_version_id,
        retry_queued_at=retry_queued_at,
        retry_lease_expires_at=retry_lease_expires_at,
        retry_attempt_token=retry_attempt_token,
        source=source,
    )


def _start_correction_retry_attempt_in_transaction(
    transaction,
    conversation_ref,
    audit_ref,
    *,
    uid: str,
    conversation_id: str,
    correction_id: str,
    recorded_base_version_id: str,
    retry_attempt_token: str,
    started_at: str,
) -> str:
    """Validate the current attempt without extending its absolute lease."""
    conversation_snapshot = conversation_ref.get(transaction=transaction)
    audit_snapshot = audit_ref.get(transaction=transaction)
    if not getattr(conversation_snapshot, "exists", False) or not getattr(audit_snapshot, "exists", False):
        return "missing"
    conversation = conversation_snapshot.to_dict() or {}
    audit = audit_snapshot.to_dict() or {}
    if (
        str(audit.get("uid") or "") != uid
        or str(audit.get("conversation_id") or "") != conversation_id
        or str(audit.get("correction_id") or "") != correction_id
        or str(audit.get("retry_attempt_token") or "") != retry_attempt_token
        or str(audit.get("status") or "") not in {"queued", "retry_queued", "processing"}
    ):
        return "stale_attempt"
    if str(conversation.get("active_summary_version_id") or "") != recorded_base_version_id:
        return "version_drift"
    lease_expires_at = _parse_correction_timestamp(audit.get("retry_lease_expires_at"))
    attempt_started_at = _parse_correction_timestamp(started_at)
    if lease_expires_at is not None and attempt_started_at is not None and lease_expires_at <= attempt_started_at:
        return "lease_expired"
    transaction.set(
        audit_ref,
        {
            "status": "processing",
            "retry_attempt_token": retry_attempt_token,
            "updated_at": started_at,
        },
        merge=True,
    )
    return "started"


@transactional
def _start_correction_retry_attempt_transaction(transaction, conversation_ref, audit_ref, **kwargs) -> str:
    return _start_correction_retry_attempt_in_transaction(transaction, conversation_ref, audit_ref, **kwargs)


def _start_correction_retry_attempt(
    *,
    uid: str,
    conversation_id: str,
    correction_id: str,
    recorded_base_version_id: str,
    retry_attempt_token: str,
) -> str:
    started = datetime.now(timezone.utc)
    conversation_ref = db.collection("users").document(uid).collection("conversations").document(conversation_id)
    return _start_correction_retry_attempt_transaction(
        db.transaction(),
        conversation_ref,
        _audit_ref(uid, conversation_id, correction_id),
        uid=uid,
        conversation_id=conversation_id,
        correction_id=correction_id,
        recorded_base_version_id=recorded_base_version_id,
        retry_attempt_token=retry_attempt_token,
        started_at=started.isoformat(),
    )


def _claim_canonical_reconciliation_in_transaction(
    transaction,
    conversation_ref,
    audit_ref,
    *,
    uid: str,
    conversation_id: str,
    correction_id: str,
    recorded_base_version_id: str,
    retry_queued_at: str,
    retry_lease_expires_at: str,
    retry_attempt_token: str,
) -> str:
    """Serialize repair after the exact correction source version is active."""

    conversation_snapshot = conversation_ref.get(transaction=transaction)
    audit_snapshot = audit_ref.get(transaction=transaction)
    if not getattr(conversation_snapshot, "exists", False) or not getattr(audit_snapshot, "exists", False):
        return "missing"
    conversation = conversation_snapshot.to_dict() or {}
    audit = audit_snapshot.to_dict() or {}
    if (
        str(audit.get("uid") or "") != uid
        or str(audit.get("conversation_id") or "") != conversation_id
        or str(audit.get("correction_id") or "") != correction_id
    ):
        return "identity_mismatch"
    _, corrected = _correction_summary_versions(conversation, correction_id)
    corrected_id = str(corrected.get("id") or "") if corrected else ""
    if (
        not corrected_id
        or str(corrected.get("based_on_version_id") or "") != recorded_base_version_id
        or str(conversation.get("active_summary_version_id") or "") != corrected_id
    ):
        return "version_drift"
    if str(audit.get("status") or "") == "applied":
        return "already_applied"
    lease_expires_at = _parse_correction_timestamp(audit.get("retry_lease_expires_at"))
    retry_now = _parse_correction_timestamp(retry_queued_at)
    if lease_expires_at is not None and retry_now is not None and lease_expires_at > retry_now:
        return "already_queued"
    transaction.set(
        audit_ref,
        {
            "status": "canonical_pending",
            "pending": True,
            "retry_attempt_token": retry_attempt_token,
            "retry_lease_expires_at": retry_lease_expires_at,
            "updated_at": retry_queued_at,
        },
        merge=True,
    )
    return "claimed"


@transactional
def _claim_canonical_reconciliation_transaction(transaction, conversation_ref, audit_ref, **kwargs) -> str:
    return _claim_canonical_reconciliation_in_transaction(transaction, conversation_ref, audit_ref, **kwargs)


def _claim_canonical_reconciliation(
    *,
    uid: str,
    conversation_id: str,
    correction_id: str,
    recorded_base_version_id: str,
    retry_queued_at: str,
    retry_lease_expires_at: str,
    retry_attempt_token: str,
) -> str:
    conversation_ref = db.collection("users").document(uid).collection("conversations").document(conversation_id)
    return _claim_canonical_reconciliation_transaction(
        db.transaction(),
        conversation_ref,
        _audit_ref(uid, conversation_id, correction_id),
        uid=uid,
        conversation_id=conversation_id,
        correction_id=correction_id,
        recorded_base_version_id=recorded_base_version_id,
        retry_queued_at=retry_queued_at,
        retry_lease_expires_at=retry_lease_expires_at,
        retry_attempt_token=retry_attempt_token,
    )


def _finish_canonical_reconciliation_in_transaction(
    transaction,
    conversation_ref,
    audit_ref,
    *,
    uid: str,
    conversation_id: str,
    correction_id: str,
    recorded_base_version_id: str,
    retry_attempt_token: str,
    audit_update: dict[str, Any],
) -> str:
    """Commit a repair result only while the exact token still owns it."""

    conversation_snapshot = conversation_ref.get(transaction=transaction)
    audit_snapshot = audit_ref.get(transaction=transaction)
    if not getattr(conversation_snapshot, "exists", False) or not getattr(audit_snapshot, "exists", False):
        return "missing"
    conversation = conversation_snapshot.to_dict() or {}
    audit = audit_snapshot.to_dict() or {}
    if (
        str(audit.get("uid") or "") != uid
        or str(audit.get("conversation_id") or "") != conversation_id
        or str(audit.get("correction_id") or "") != correction_id
        or str(audit.get("retry_attempt_token") or "") != retry_attempt_token
    ):
        return "stale_attempt"
    if str(audit.get("status") or "") in {"applied", "undone", "consent_revoked"}:
        return "already_terminal"
    _, corrected = _correction_summary_versions(conversation, correction_id)
    corrected_id = str(corrected.get("id") or "") if corrected else ""
    if (
        not corrected_id
        or str(corrected.get("based_on_version_id") or "") != recorded_base_version_id
        or str(conversation.get("active_summary_version_id") or "") != corrected_id
    ):
        return "version_drift"
    transaction.set(audit_ref, audit_update, merge=True)
    state = conversation.get("correction_state") or {}
    if str(state.get("correction_id") or "") == correction_id:
        status_value = str(audit_update.get("status") or state.get("status") or "canonical_pending")
        state_update = {
            **state,
            "status": status_value,
            "pending": bool(audit_update.get("pending", status_value != "applied")),
            "updated_at": audit_update.get("updated_at"),
            "failure_code": audit_update.get("failure_code"),
            "active_summary_version_id": corrected_id,
            "retry_attempt_token": retry_attempt_token,
        }
        conversation_update: dict[str, Any] = {"correction_state": state_update}
        if status_value == "applied":
            enrichment = conversation.get("enrichment_state") or {}
            conversation_update["enrichment_state"] = {
                **enrichment,
                "status": "writeback_applied",
                "pending": False,
                "canonical_status": "completed",
                "error": None,
                "updated_at": audit_update.get("updated_at"),
            }
        transaction.set(conversation_ref, conversation_update, merge=True)
    return "finished"


@transactional
def _finish_canonical_reconciliation_transaction(transaction, conversation_ref, audit_ref, **kwargs) -> str:
    return _finish_canonical_reconciliation_in_transaction(transaction, conversation_ref, audit_ref, **kwargs)


def _finish_canonical_reconciliation(
    *,
    uid: str,
    conversation_id: str,
    correction_id: str,
    recorded_base_version_id: str,
    retry_attempt_token: str,
    audit_update: dict[str, Any],
) -> str:
    conversation_ref = db.collection("users").document(uid).collection("conversations").document(conversation_id)
    return _finish_canonical_reconciliation_transaction(
        db.transaction(),
        conversation_ref,
        _audit_ref(uid, conversation_id, correction_id),
        uid=uid,
        conversation_id=conversation_id,
        correction_id=correction_id,
        recorded_base_version_id=recorded_base_version_id,
        retry_attempt_token=retry_attempt_token,
        audit_update=audit_update,
    )


def _record_failed_correction_attempt_in_transaction(
    transaction,
    conversation_ref,
    audit_ref,
    *,
    uid: str,
    conversation_id: str,
    correction_id: str,
    retry_attempt_token: Optional[str],
    audit_update: dict[str, Any],
    correction_state: dict[str, Any],
) -> str:
    """Write failure only while this token owns an uncommitted correction."""

    conversation_snapshot = conversation_ref.get(transaction=transaction)
    audit_snapshot = audit_ref.get(transaction=transaction)
    if not getattr(conversation_snapshot, "exists", False) or not getattr(audit_snapshot, "exists", False):
        return "missing"
    conversation = conversation_snapshot.to_dict() or {}
    audit = audit_snapshot.to_dict() or {}
    if (
        str(audit.get("uid") or "") != uid
        or str(audit.get("conversation_id") or "") != conversation_id
        or str(audit.get("correction_id") or "") != correction_id
    ):
        return "identity_mismatch"
    if retry_attempt_token and str(audit.get("retry_attempt_token") or "") != retry_attempt_token:
        return "stale_attempt"
    _, corrected = _correction_summary_versions(conversation, correction_id)
    corrected_id = str(corrected.get("id") or "") if corrected else ""
    if corrected_id and str(conversation.get("active_summary_version_id") or "") == corrected_id:
        return "irreversible_success"
    if str(audit.get("status") or "") in {"applied", "undone", "consent_revoked"}:
        return "already_terminal"
    transaction.set(audit_ref, audit_update, merge=True)
    state = conversation.get("correction_state") or {}
    if str(state.get("correction_id") or "") == correction_id:
        transaction.set(conversation_ref, {"correction_state": correction_state}, merge=True)
    return "recorded"


@transactional
def _record_failed_correction_attempt_transaction(transaction, conversation_ref, audit_ref, **kwargs) -> str:
    return _record_failed_correction_attempt_in_transaction(transaction, conversation_ref, audit_ref, **kwargs)


def _record_failed_correction_attempt(
    *,
    uid: str,
    conversation_id: str,
    correction_id: str,
    retry_attempt_token: Optional[str],
    audit_update: dict[str, Any],
    correction_state: dict[str, Any],
) -> str:
    conversation_ref = db.collection("users").document(uid).collection("conversations").document(conversation_id)
    return _record_failed_correction_attempt_transaction(
        db.transaction(),
        conversation_ref,
        _audit_ref(uid, conversation_id, correction_id),
        uid=uid,
        conversation_id=conversation_id,
        correction_id=correction_id,
        retry_attempt_token=retry_attempt_token,
        audit_update=audit_update,
        correction_state=correction_state,
    )


def _reconcile_committed_correction(
    *,
    uid: str,
    conversation_id: str,
    correction_id: str,
    trace_id: str,
    active_summary_version_id: Optional[str],
    proposal_id: Optional[str],
    expected_retry_attempt_token: Optional[str],
) -> Optional[ConversationCorrectionResponse]:
    """Derive a non-failure receipt from source/canonical durable evidence."""

    conversation = conversations_db.get_conversation(uid, conversation_id)
    if conversation is None:
        return None
    _, corrected = _correction_summary_versions(conversation, correction_id)
    if corrected is None:
        return None
    corrected_id = str(corrected.get("id") or "")
    if (
        not corrected_id
        or str(corrected.get("based_on_version_id") or "") != str(active_summary_version_id or "")
        or str(conversation.get("active_summary_version_id") or "") != corrected_id
    ):
        return None
    audit = _read_correction_audit(uid, conversation_id, correction_id)
    if not expected_retry_attempt_token or str(audit.get("retry_attempt_token") or "") != expected_retry_attempt_token:
        return None
    enrichment = conversation.get("enrichment_state") or {}
    audit_apply_result = audit.get("direct_apply_result") or {}
    canonical_confirmed = bool(
        str(enrichment.get("trace_id") or "") == trace_id
        and enrichment.get("status") == "writeback_applied"
        and enrichment.get("canonical_status") == "completed"
    ) or bool(isinstance(audit_apply_result, dict) and audit_apply_result.get("canonical_confirmed") is True)
    status_value = (
        "applied" if canonical_confirmed or str(audit.get("status") or "") == "applied" else "canonical_pending"
    )
    reconciled_at = _now_iso()
    try:
        finish_status = _finish_canonical_reconciliation(
            uid=uid,
            conversation_id=conversation_id,
            correction_id=correction_id,
            recorded_base_version_id=str(active_summary_version_id or ""),
            retry_attempt_token=expected_retry_attempt_token,
            audit_update={
                "status": status_value,
                "pending": status_value != "applied",
                "updated_at": reconciled_at,
                "applied_at": audit.get("applied_at") or (reconciled_at if status_value == "applied" else None),
                "failure_code": None if status_value == "applied" else "canonical_write_unconfirmed",
                "retry_lease_expires_at": None,
            },
        )
        if finish_status != "finished":
            return None
    except Exception:
        logger.exception(
            "Failed to persist reconciled correction receipt",
            extra={"uid": uid, "conversation_id": conversation_id, "correction_id": correction_id},
        )
        return None
    return ConversationCorrectionResponse(
        correction_id=correction_id,
        conversation_id=conversation_id,
        trace_id=trace_id,
        status=status_value,
        queued=status_value != "applied",
        proposal_id=proposal_id,
    )


async def _retry_failed_conversation_correction(
    *,
    uid: str,
    conversation_id: str,
    correction_id: str,
    background_tasks: Optional[BackgroundTasks],
    deadline: Optional[_CorrectionDeadline] = None,
) -> ConversationCorrectionResponse:
    deadline = deadline or _CorrectionDeadline()
    conversation, audit, recorded_base_version_id = await deadline.run_blocking(
        _exact_correction_retry_context,
        uid=uid,
        conversation_id=conversation_id,
        correction_id=correction_id,
    )
    _, corrected = _correction_summary_versions(conversation, correction_id)
    if corrected is not None:
        corrected_version_id = str(corrected.get("id") or "")
        if (
            str(corrected.get("based_on_version_id") or "") != recorded_base_version_id
            or str(conversation.get("active_summary_version_id") or "") != corrected_version_id
        ):
            raise HTTPException(status_code=409, detail="Conversation summary changed after correction")
    elif str(conversation.get("active_summary_version_id") or "") != recorded_base_version_id:
        raise HTTPException(status_code=409, detail="Conversation summary changed after correction")

    consent_terminal = await deadline.run_blocking(
        _require_correction_consent_or_terminal_response,
        uid=uid,
        conversation_id=conversation_id,
        correction_id=correction_id,
        expected_retry_attempt_token=str(audit.get("retry_attempt_token") or "") or None,
    )
    if consent_terminal is not None:
        return consent_terminal

    trace_id = str(audit.get("trace_id") or f"correction:{conversation_id}:{correction_id}")
    submitted_at = str(audit.get("created_at") or audit.get("submitted_at") or _now_iso())
    try:
        request = ConversationCorrectionRequest(
            correction_text=str(audit.get("correction_text") or ""),
            source=str(audit.get("source") or "ios"),
            summary_context=audit.get("summary_context") if isinstance(audit.get("summary_context"), dict) else {},
        )
    except Exception as exc:
        raise HTTPException(status_code=409, detail="Correction retry metadata is invalid") from exc
    structured = audit.get("current_summary") if isinstance(audit.get("current_summary"), dict) else {}
    transcript = _format_transcript(conversation)
    segment_count = len(conversation.get("transcript_segments") or [])

    if corrected is not None:
        if str(audit.get("status") or "") == "applied":
            return _correction_response_from_audit(
                conversation_id=conversation_id,
                correction_id=correction_id,
                audit=audit,
            )
        reconciliation_now = datetime.now(timezone.utc)
        reconciliation_attempt_token = _new_correction_attempt_token()
        claim_status = await deadline.run_blocking(
            _claim_canonical_reconciliation,
            uid=uid,
            conversation_id=conversation_id,
            correction_id=correction_id,
            recorded_base_version_id=recorded_base_version_id,
            retry_queued_at=reconciliation_now.isoformat(),
            retry_lease_expires_at=_correction_retry_lease_expiry(reconciliation_now),
            retry_attempt_token=reconciliation_attempt_token,
        )
        if claim_status in {"already_applied", "already_queued"}:
            return _correction_response_from_audit(
                conversation_id=conversation_id,
                correction_id=correction_id,
                audit=_read_correction_audit(uid, conversation_id, correction_id),
            )
        if claim_status == "missing":
            raise HTTPException(status_code=404, detail="Correction not found")
        if claim_status in {"identity_mismatch", "version_drift"}:
            raise HTTPException(status_code=409, detail="Conversation summary changed after correction")
        if claim_status != "claimed":
            raise HTTPException(status_code=500, detail="Canonical correction reconciliation claim failed")

        enrichment = conversation.get("enrichment_state") or {}
        audit_apply_result = audit.get("direct_apply_result") or {}
        canonical_already_confirmed = bool(
            isinstance(enrichment, dict)
            and str(enrichment.get("trace_id") or "") == trace_id
            and enrichment.get("status") == "writeback_applied"
            and enrichment.get("canonical_status") == "completed"
        ) or bool(isinstance(audit_apply_result, dict) and audit_apply_result.get("canonical_confirmed") is True)
        if canonical_already_confirmed:
            apply_result = dict(audit_apply_result) if isinstance(audit_apply_result, dict) else {}
            apply_result.setdefault("status", "ok")
            apply_result.setdefault("active_summary_version_id", corrected_version_id)
            apply_result["canonical_confirmed"] = True
        else:
            try:
                await deadline.run_blocking(require_current_ai_consent, uid)
                apply_result = await deadline.run_async(
                    lambda: _apply_corrected_summary(
                        uid=uid,
                        conversation_id=conversation_id,
                        correction_id=correction_id,
                        trace_id=trace_id,
                        active_summary_version_id=recorded_base_version_id,
                        corrected=corrected,
                        retry_attempt_token=reconciliation_attempt_token,
                    )
                )
                if apply_result.get("canonical_confirmed") is not True:
                    raise CanonicalSummaryWriteUnconfirmedError("canonical_write_unconfirmed")
            except HTTPException as error:
                detail = error.detail if isinstance(error.detail, dict) else {}
                if error.status_code != 403 or detail.get("code") != "ai_consent_required":
                    raise
                terminal = _require_correction_consent_or_terminal_response(
                    uid=uid,
                    conversation_id=conversation_id,
                    correction_id=correction_id,
                    expected_retry_attempt_token=reconciliation_attempt_token,
                )
                if terminal is not None:
                    return terminal
                raise
            except (CanonicalSummaryWriteUnconfirmedError, TimeoutError):
                pending_at = _now_iso()
                try:
                    finish_status = await deadline.run_blocking(
                        _finish_canonical_reconciliation,
                        uid=uid,
                        conversation_id=conversation_id,
                        correction_id=correction_id,
                        recorded_base_version_id=recorded_base_version_id,
                        retry_attempt_token=reconciliation_attempt_token,
                        audit_update={
                            "status": "canonical_pending",
                            "pending": True,
                            "updated_at": pending_at,
                            "failure_code": "canonical_write_unconfirmed",
                            "retry_lease_expires_at": None,
                        },
                    )
                except Exception:
                    finish_status = "confirmation_failed"
                    logger.exception(
                        "Failed to persist canonical reconciliation pending marker",
                        extra={"uid": uid, "conversation_id": conversation_id, "correction_id": correction_id},
                    )
                if finish_status != "finished":
                    reconciled = await deadline.run_cleanup_blocking(
                        _reconcile_committed_correction,
                        uid=uid,
                        conversation_id=conversation_id,
                        correction_id=correction_id,
                        trace_id=trace_id,
                        active_summary_version_id=recorded_base_version_id,
                        proposal_id=str(audit.get("proposal_id") or "") or None,
                        expected_retry_attempt_token=reconciliation_attempt_token,
                    )
                    if reconciled is not None:
                        return reconciled
                    return ConversationCorrectionResponse(
                        conversation_id=conversation_id,
                        correction_id=correction_id,
                        trace_id=trace_id,
                        status="canonical_pending",
                        queued=True,
                        proposal_id=str(audit.get("proposal_id") or "") or None,
                    )
                return ConversationCorrectionResponse(
                    correction_id=correction_id,
                    conversation_id=conversation_id,
                    trace_id=trace_id,
                    status="canonical_pending",
                    queued=True,
                    proposal_id=str(audit.get("proposal_id") or "") or None,
                )
        applied_at = _now_iso()
        try:
            finish_status = await deadline.run_blocking(
                _finish_canonical_reconciliation,
                uid=uid,
                conversation_id=conversation_id,
                correction_id=correction_id,
                recorded_base_version_id=recorded_base_version_id,
                retry_attempt_token=reconciliation_attempt_token,
                audit_update={
                    "status": "applied",
                    "pending": False,
                    "applied_at": applied_at,
                    "updated_at": applied_at,
                    "direct_apply_result": apply_result,
                    "failure_code": None,
                    "retry_lease_expires_at": None,
                },
            )
        except Exception:
            finish_status = "confirmation_failed"
            logger.exception(
                "Canonical correction durable before reconciliation receipt confirmation",
                extra={"uid": uid, "conversation_id": conversation_id, "correction_id": correction_id},
            )
        if finish_status != "finished":
            reconciled = await deadline.run_cleanup_blocking(
                _reconcile_committed_correction,
                uid=uid,
                conversation_id=conversation_id,
                correction_id=correction_id,
                trace_id=trace_id,
                active_summary_version_id=recorded_base_version_id,
                proposal_id=str(audit.get("proposal_id") or "") or None,
                expected_retry_attempt_token=reconciliation_attempt_token,
            )
            if reconciled is not None and reconciled.status == "applied":
                return reconciled
            return ConversationCorrectionResponse(
                conversation_id=conversation_id,
                correction_id=correction_id,
                trace_id=trace_id,
                status="applied",
                queued=False,
                proposal_id=str(audit.get("proposal_id") or "") or None,
            )
        if not _audit_has_completed_stage(audit, "direct_apply_succeeded"):
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
                    "reconciled": True,
                },
            )
        proposal_id = str(audit.get("proposal_id") or "") or None
        try:
            require_current_ai_consent(uid)
            proposal_id = await _run_post_source_correction_side_effects(
                uid=uid,
                conversation_id=conversation_id,
                correction_id=correction_id,
                trace_id=trace_id,
                request=request,
                source_conversation=conversation,
                structured=structured,
                transcript=transcript,
                segment_count=segment_count,
                submitted_at=submitted_at,
                active_summary_version_id=recorded_base_version_id,
            )
        except HTTPException as error:
            detail = error.detail if isinstance(error.detail, dict) else {}
            if error.status_code != 403 or detail.get("code") != "ai_consent_required":
                raise
        return ConversationCorrectionResponse(
            correction_id=correction_id,
            conversation_id=conversation_id,
            trace_id=trace_id,
            status="applied",
            queued=False,
            proposal_id=proposal_id,
        )

    audit_status = str(audit.get("status") or "")
    if audit_status not in {
        "submitted",
        "queued",
        "processing",
        "retry_queued",
        "canonical_pending",
        "direct_apply_failed",
        "direct_apply_disabled",
        "queue_failed",
    }:
        raise HTTPException(status_code=409, detail="Correction is not retryable")

    retry_now = datetime.now(timezone.utc)
    retry_queued_at = retry_now.isoformat()
    retry_lease_expires_at = _correction_retry_lease_expiry(retry_now)
    retry_attempt_token = _new_correction_attempt_token()
    claim_status = await deadline.run_blocking(
        _claim_failed_correction_retry,
        uid=uid,
        conversation_id=conversation_id,
        correction_id=correction_id,
        recorded_base_version_id=recorded_base_version_id,
        retry_queued_at=retry_queued_at,
        retry_lease_expires_at=retry_lease_expires_at,
        retry_attempt_token=retry_attempt_token,
        source=request.source,
    )
    if claim_status == "version_drift":
        raise HTTPException(status_code=409, detail="Conversation summary changed after correction")
    if claim_status == "already_queued":
        return ConversationCorrectionResponse(
            correction_id=correction_id,
            conversation_id=conversation_id,
            trace_id=trace_id,
            status="queued",
            queued=True,
            proposal_id=str(audit.get("proposal_id") or "") or None,
        )
    if claim_status == "missing":
        raise HTTPException(status_code=404, detail="Correction not found")
    if claim_status in {"identity_mismatch", "not_retryable"}:
        raise HTTPException(status_code=409, detail="Correction is not retryable")
    if claim_status != "claimed":
        raise HTTPException(status_code=500, detail="Correction retry claim failed")
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
        "active_summary_version_id": recorded_base_version_id,
        "proposal_id": str(audit.get("proposal_id") or "") or None,
        "source_conversation": conversation,
        "retry_attempt_token": retry_attempt_token,
        "retry_deadline_at": retry_lease_expires_at,
        "deadline": deadline,
    }
    if background_tasks is not None:
        background_tasks.add_task(_run_direct_correction_apply, **direct_apply_kwargs)
    else:
        return await _run_direct_correction_apply(**direct_apply_kwargs)
    return ConversationCorrectionResponse(
        correction_id=correction_id,
        conversation_id=conversation_id,
        trace_id=trace_id,
        status="queued",
        queued=True,
        proposal_id=str(audit.get("proposal_id") or "") or None,
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
async def get_conversation_correction_receipt(
    conversation_id: str,
    correction_id: str,
    background_tasks: BackgroundTasks,
    uid: str = Depends(get_exact_firebase_uid),
) -> ConversationCorrectionReceiptResponse:
    conversation = conversations_db.get_conversation(uid, conversation_id)
    receipt = _correction_receipt(
        uid=uid,
        conversation_id=conversation_id,
        correction_id=correction_id,
        conversation=conversation,
    )
    audit = _read_correction_audit(uid, conversation_id, correction_id)
    if _correction_work_lease_is_reclaimable(audit, now=datetime.now(timezone.utc)):
        # Polling the exact authenticated receipt is the durable recovery path
        # for an initial background task lost after its queued audit committed.
        # The transactional claim below fences concurrent polls and live leases.
        await _retry_failed_conversation_correction(
            uid=uid,
            conversation_id=conversation_id,
            correction_id=correction_id,
            background_tasks=background_tasks,
        )
        return _correction_receipt(
            uid=uid,
            conversation_id=conversation_id,
            correction_id=correction_id,
        )
    return receipt


@router.post(
    "/v1/ella/conversations/{conversation_id}/corrections/{correction_id}/retry",
    response_model=ConversationCorrectionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_failed_conversation_correction(
    conversation_id: str,
    correction_id: str,
    background_tasks: BackgroundTasks,
    uid: str = Depends(get_exact_firebase_uid),
) -> ConversationCorrectionResponse:
    return await _retry_failed_conversation_correction(
        uid=uid,
        conversation_id=conversation_id,
        correction_id=correction_id,
        background_tasks=background_tasks,
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
