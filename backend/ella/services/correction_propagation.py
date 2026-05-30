"""Conservative OMI correction propagation worker.

OMI keeps evidence, audit records, candidate decisions, and rollback pointers.
Durable user facts remain owned by Hermes/Honcho and are represented here as
proposal records until that write contract is explicit.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from pydantic import BaseModel, Field

from database._client import db
from ella.services import proposal_ingest
from ella.services.correction_honcho_contract import (
    HONCHO_FACT_PROPOSALS_ENABLED,
    build_honcho_fact_candidate,
    create_honcho_fact_candidate_proposal,
)

PROPAGATION_TOOL_NAME = "omi_correction_propagation_propose"
STOPWORDS = {
    "about",
    "actually",
    "after",
    "also",
    "and",
    "are",
    "audio",
    "before",
    "conversation",
    "correct",
    "correction",
    "did",
    "for",
    "from",
    "had",
    "has",
    "have",
    "not",
    "omi",
    "please",
    "said",
    "summary",
    "that",
    "the",
    "this",
    "was",
    "were",
    "with",
}
TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9'_-]{2,}", re.I)


class CorrectionPropagationDecision(BaseModel):
    conversation_id: str = ""
    action: str
    reason: str = ""
    confidence: float = 0.0
    skipped_reasons: list[str] = Field(default_factory=list)
    overlap_terms: list[str] = Field(default_factory=list)
    idempotency_key: str = ""
    proposal_id: str = ""
    active_summary_version_id: str = ""
    rollback_ref: dict[str, Any] = Field(default_factory=dict)


class CorrectionPropagationRun(BaseModel):
    run_id: str
    uid: str
    source_conversation_id: str
    correction_id: str
    trace_id: str
    correction_type: str
    status: str = "success"
    dry_run: bool = False
    started_at: datetime
    completed_at: datetime
    latency_ms: int = 0
    candidate_count: int = 0
    proposal_count: int = 0
    honcho_fact_candidate_count: int = 0
    honcho_fact_proposal_count: int = 0
    skipped_count: int = 0
    auto_applied_count: int = 0
    decisions: list[CorrectionPropagationDecision] = Field(default_factory=list)
    model_metadata: dict[str, Any] = Field(default_factory=dict)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stable_json(value: Any) -> str:
    return json.dumps(value or {}, sort_keys=True, separators=(",", ":"), default=str)


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()[:16]


def _as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif hasattr(value, "timestamp"):
        try:
            parsed = datetime.fromtimestamp(value.timestamp(), timezone.utc)
        except Exception:
            return None
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _conversation_time(conversation: dict[str, Any]) -> datetime:
    for key in ("started_at", "created_at", "finished_at", "updated_at"):
        parsed = _as_datetime(conversation.get(key))
        if parsed:
            return parsed
    return _now()


def _summary_text(conversation: dict[str, Any]) -> str:
    structured = conversation.get("structured") if isinstance(conversation.get("structured"), dict) else {}
    return " ".join(
        str(value or "")
        for value in (
            conversation.get("title"),
            conversation.get("overview"),
            structured.get("title"),
            structured.get("overview"),
            structured.get("category"),
            conversation.get("category"),
        )
    )


def _transcript_text(conversation: dict[str, Any], max_segments: int = 30) -> str:
    segments = conversation.get("transcript_segments") or []
    if not isinstance(segments, list):
        return ""
    return " ".join(str(segment.get("text") or "") for segment in segments[:max_segments] if isinstance(segment, dict))


def _terms(*values: str) -> set[str]:
    raw = " ".join(values).lower()
    return {token for token in TOKEN_RE.findall(raw) if token not in STOPWORDS and len(token) >= 3}


def _active_summary_version(conversation: dict[str, Any]) -> str:
    return str(conversation.get("active_summary_version_id") or "")


def _active_version_compatible(conversation: dict[str, Any]) -> bool:
    active = _active_summary_version(conversation)
    if not active:
        return True
    versions = conversation.get("summary_versions") or []
    if not versions:
        return True
    for version in versions:
        if not isinstance(version, dict):
            continue
        if str(version.get("id") or "") == active:
            return bool(version.get("is_active", True))
    return False


def _conversation_id(conversation: dict[str, Any]) -> str:
    return str(conversation.get("id") or conversation.get("conversation_id") or "")


def _source_ref(conversation: dict[str, Any]) -> dict[str, Any]:
    return {
        "conversation_id": _conversation_id(conversation),
        "active_summary_version_id": _active_summary_version(conversation),
        "created_at": _conversation_time(conversation).isoformat(),
    }


def _load_related_conversations(uid: str, source_conversation: dict[str, Any], *, limit: int) -> list[dict[str, Any]]:
    """Best-effort Firestore bounded same-user loader.

    Unit tests can inject a candidate_loader; production keeps this narrow and
    returns [] if Firestore cannot satisfy the local query.
    """
    source_at = _conversation_time(source_conversation)
    window_hours = max(1, min(int(os.getenv("ELLA_CORRECTION_PROPAGATION_WINDOW_HOURS", "12")), 48))
    start = source_at - timedelta(hours=window_hours)
    end = source_at + timedelta(hours=window_hours)
    try:
        query = (
            db.collection("users")
            .document(uid)
            .collection("conversations")
            .where("created_at", ">=", start)
            .where("created_at", "<=", end)
            .limit(limit)
        )
        conversations: list[dict[str, Any]] = []
        for snapshot in query.stream():
            data = snapshot.to_dict() or {}
            data.setdefault("id", getattr(snapshot, "id", ""))
            conversations.append(data)
        return conversations
    except Exception:
        return []


def _score_candidate(
    *,
    source_terms: set[str],
    source_time: datetime,
    correction_type: str,
    candidate: dict[str, Any],
) -> tuple[float, list[str], list[str]]:
    skipped: list[str] = []
    candidate_time = _conversation_time(candidate)
    delta_seconds = abs((candidate_time - source_time).total_seconds())
    window_hours = max(1, min(int(os.getenv("ELLA_CORRECTION_PROPAGATION_WINDOW_HOURS", "12")), 48))
    if delta_seconds > window_hours * 3600:
        skipped.append("outside_local_window")
    if not _active_version_compatible(candidate):
        skipped.append("active_summary_version_incompatible")

    candidate_terms = _terms(_summary_text(candidate), _transcript_text(candidate, max_segments=20))
    overlap = sorted(source_terms & candidate_terms)
    if not overlap:
        skipped.append("no_keyword_entity_overlap")

    overlap_score = min(1.0, len(overlap) / max(3, min(len(source_terms), 10)))
    same_day_score = 1.0 if source_time.date() == candidate_time.date() else 0.0
    source_score = (
        1.0 if str(candidate.get("source") or candidate.get("channel") or "omi").lower() in {"omi", "necklace"} else 0.5
    )
    correction_score = 0.1 if correction_type in {"identity", "media", "topic", "title"} else 0.0
    confidence = min(1.0, 0.42 * overlap_score + 0.28 * same_day_score + 0.2 * source_score + correction_score)
    return round(confidence, 3), skipped, overlap[:20]


def _claims(uid: str, trace_id: str) -> dict[str, Any]:
    return {
        "sub": "ella-correction-observer",
        "profile_uid": uid,
        "role": "system_observer",
        "external_provider": "omi_backend",
        "grant_id": "omi-correction-propagation",
        "trace_id": trace_id,
        "scopes": ["proposals:write"],
        "allowed_tools": [PROPAGATION_TOOL_NAME],
    }


def _proposal_payload(
    *,
    uid: str,
    source_conversation: dict[str, Any],
    candidate: dict[str, Any],
    correction_id: str,
    correction_text: str,
    correction_type: str,
    trace_id: str,
    confidence: float,
    overlap_terms: list[str],
) -> dict[str, Any]:
    candidate_id = _conversation_id(candidate)
    source_id = _conversation_id(source_conversation)
    return {
        "title": f"Propagate OMI correction to related conversation {candidate_id}",
        "description": correction_text,
        "target": {
            "kind": "omi_conversation_summary",
            "conversation_id": candidate_id,
            "active_summary_version_id": _active_summary_version(candidate),
            "correction_id": correction_id,
        },
        "evidence": [
            {
                "kind": "source_correction",
                "uid": uid,
                "conversation_id": source_id,
                "correction_id": correction_id,
                "trace_id": trace_id,
                "correction_text": correction_text,
                "correction_type": correction_type,
            },
            {"kind": "source_conversation_summary", "content": _summary_text(source_conversation)[:2000]},
            {"kind": "candidate_conversation_summary", "content": _summary_text(candidate)[:2000]},
            {"kind": "candidate_transcript_excerpt", "content": _transcript_text(candidate, max_segments=12)[:3000]},
            {"kind": "overlap_terms", "terms": overlap_terms},
        ],
        "requested_change": {
            "correction_text": correction_text,
            "correction_type": correction_type,
            "source_conversation_id": source_id,
            "related_conversation_id": candidate_id,
            "confidence": confidence,
            "durable_owner": "honcho/hermes",
            "honcho_write_contract": "pending",
        },
        "source": "omi_correction_observer",
        "write_policy": "proposal_only",
        "reason": "Bounded same-user OMI correction propagation candidate.",
        "confidence": confidence,
    }


def _dump_run(run: CorrectionPropagationRun) -> dict[str, Any]:
    if hasattr(run, "model_dump"):
        return run.model_dump(mode="json")
    return run.dict()


def run_correction_propagation(
    *,
    uid: str,
    source_conversation: dict[str, Any],
    correction_id: str,
    trace_id: str,
    correction_text: str,
    correction_type: str,
    candidate_loader: Callable[..., list[dict[str, Any]]] | None = None,
    create_proposal: Callable[..., dict[str, Any]] = proposal_ingest.create_proposal,
    dry_run: bool = False,
    limit: int = 25,
    min_confidence: float | None = None,
) -> CorrectionPropagationRun:
    started = _now()
    monotonic_start = time.monotonic()
    source_id = _conversation_id(source_conversation)
    run_id = f"omi-correction-propagation:{uid}:{source_id}:{correction_id}"
    confidence_threshold = (
        min_confidence
        if min_confidence is not None
        else float(os.getenv("ELLA_CORRECTION_PROPAGATION_MIN_CONFIDENCE", "0.82"))
    )
    candidate_limit = max(1, min(int(limit or 25), 100))
    loader = candidate_loader or _load_related_conversations
    candidates = loader(uid, source_conversation, limit=candidate_limit)
    source_time = _conversation_time(source_conversation)
    source_terms = _terms(correction_text, _summary_text(source_conversation), _transcript_text(source_conversation))
    decisions: list[CorrectionPropagationDecision] = []
    proposal_count = 0
    honcho_fact_candidate_count = 0
    honcho_fact_proposal_count = 0

    for candidate in candidates:
        candidate_id = _conversation_id(candidate)
        if candidate_id == source_id:
            decisions.append(
                CorrectionPropagationDecision(conversation_id=candidate_id, action="skip", reason="source_conversation")
            )
            continue
        candidate_uid = str(candidate.get("uid") or candidate.get("profile_uid") or uid)
        if candidate_uid != uid:
            decisions.append(
                CorrectionPropagationDecision(
                    conversation_id=candidate_id,
                    action="skip",
                    reason="cross_user_candidate",
                    skipped_reasons=["cross_user_candidate"],
                )
            )
            continue

        confidence, skipped, overlap = _score_candidate(
            source_terms=source_terms,
            source_time=source_time,
            correction_type=correction_type,
            candidate=candidate,
        )
        if skipped or confidence < confidence_threshold:
            decisions.append(
                CorrectionPropagationDecision(
                    conversation_id=candidate_id,
                    action="skip",
                    reason="candidate_below_threshold" if confidence < confidence_threshold else skipped[0],
                    confidence=confidence,
                    skipped_reasons=skipped,
                    overlap_terms=overlap,
                    active_summary_version_id=_active_summary_version(candidate),
                    rollback_ref=_source_ref(candidate),
                )
            )
            continue

        payload = _proposal_payload(
            uid=uid,
            source_conversation=source_conversation,
            candidate=candidate,
            correction_id=correction_id,
            correction_text=correction_text,
            correction_type=correction_type,
            trace_id=trace_id,
            confidence=confidence,
            overlap_terms=overlap,
        )
        idempotency_key = f"omi-correction-propagation:{uid}:{correction_id}:{candidate_id}:{_stable_hash(payload)}"
        fact_candidate = build_honcho_fact_candidate(
            uid=uid,
            source_conversation=source_conversation,
            related_conversation=candidate,
            correction_id=correction_id,
            trace_id=trace_id,
            correction_text=correction_text,
            correction_type=correction_type,
            confidence=confidence,
        )
        if fact_candidate is not None:
            honcho_fact_candidate_count += 1
        if dry_run:
            decisions.append(
                CorrectionPropagationDecision(
                    conversation_id=candidate_id,
                    action="would_create_proposal",
                    reason="high_confidence_same_user_candidate",
                    confidence=confidence,
                    overlap_terms=overlap,
                    idempotency_key=idempotency_key,
                    active_summary_version_id=_active_summary_version(candidate),
                    rollback_ref=_source_ref(candidate),
                )
            )
            if fact_candidate is not None and HONCHO_FACT_PROPOSALS_ENABLED:
                decisions.append(
                    CorrectionPropagationDecision(
                        conversation_id=candidate_id,
                        action="would_create_honcho_fact_proposal",
                        reason="high_confidence_correction_fact_candidate",
                        confidence=confidence,
                        overlap_terms=overlap,
                        idempotency_key=fact_candidate.idempotency_key,
                        active_summary_version_id=_active_summary_version(candidate),
                        rollback_ref=_source_ref(candidate),
                    )
                )
                honcho_fact_proposal_count += 1
            proposal_count += 1
            continue

        try:
            result = create_proposal(
                session_claims=_claims(uid, trace_id),
                tool_name=PROPAGATION_TOOL_NAME,
                proposal_type="summary_correction",
                payload=payload,
                idempotency_key=idempotency_key,
            )
            proposal = result.get("proposal") or {}
            proposal_id = str(proposal.get("proposal_id") or "")
            decisions.append(
                CorrectionPropagationDecision(
                    conversation_id=candidate_id,
                    action="created_proposal" if result.get("created") else "deduped_proposal",
                    reason="high_confidence_same_user_candidate",
                    confidence=confidence,
                    overlap_terms=overlap,
                    idempotency_key=idempotency_key,
                    proposal_id=proposal_id,
                    active_summary_version_id=_active_summary_version(candidate),
                    rollback_ref=_source_ref(candidate),
                )
            )
            proposal_count += 1
            if fact_candidate is not None:
                fact_result = create_honcho_fact_candidate_proposal(fact_candidate, create_proposal=create_proposal)
                fact_proposal = fact_result.get("proposal") or {}
                fact_proposal_id = str(fact_proposal.get("proposal_id") or "")
                if fact_result.get("skipped"):
                    action = "honcho_fact_proposal_skipped"
                    reason = str(fact_result.get("reason") or "honcho_fact_proposal_skipped")
                else:
                    action = (
                        "created_honcho_fact_proposal" if fact_result.get("created") else "deduped_honcho_fact_proposal"
                    )
                    reason = "high_confidence_correction_fact_candidate"
                    honcho_fact_proposal_count += 1
                decisions.append(
                    CorrectionPropagationDecision(
                        conversation_id=candidate_id,
                        action=action,
                        reason=reason,
                        confidence=confidence,
                        overlap_terms=overlap,
                        idempotency_key=fact_candidate.idempotency_key,
                        proposal_id=fact_proposal_id,
                        active_summary_version_id=_active_summary_version(candidate),
                        rollback_ref=_source_ref(candidate),
                    )
                )
        except Exception as exc:
            decisions.append(
                CorrectionPropagationDecision(
                    conversation_id=candidate_id,
                    action="error",
                    reason="proposal_create_failed",
                    confidence=confidence,
                    skipped_reasons=[str(exc)],
                    overlap_terms=overlap,
                    idempotency_key=idempotency_key,
                    active_summary_version_id=_active_summary_version(candidate),
                    rollback_ref=_source_ref(candidate),
                )
            )

    completed = _now()
    skipped_count = sum(1 for decision in decisions if decision.action == "skip")
    error_count = sum(1 for decision in decisions if decision.action == "error")
    return CorrectionPropagationRun(
        run_id=run_id,
        uid=uid,
        source_conversation_id=source_id,
        correction_id=correction_id,
        trace_id=trace_id,
        correction_type=correction_type,
        status="error" if error_count else "success",
        dry_run=dry_run,
        started_at=started,
        completed_at=completed,
        latency_ms=int((time.monotonic() - monotonic_start) * 1000),
        candidate_count=len(candidates),
        proposal_count=proposal_count,
        honcho_fact_candidate_count=honcho_fact_candidate_count,
        honcho_fact_proposal_count=honcho_fact_proposal_count,
        skipped_count=skipped_count,
        auto_applied_count=0,
        decisions=decisions,
        model_metadata={
            "candidate_bounds": {
                "same_uid_only": True,
                "window_hours": int(os.getenv("ELLA_CORRECTION_PROPAGATION_WINDOW_HOURS", "12")),
                "min_confidence": confidence_threshold,
                "source_preference": "omi/necklace",
            },
            "durable_owner": "honcho/hermes",
            "write_policy": "proposal_only",
            "honcho_fact_contract": {
                "enabled": HONCHO_FACT_PROPOSALS_ENABLED,
                "proposal_type": "memory_note",
                "write_default": "disabled",
            },
        },
    )


def propagation_run_to_dict(run: CorrectionPropagationRun) -> dict[str, Any]:
    return _dump_run(run)
