"""Ella Observer fact-promotion runner.

The Observer is the domain worker that reads canonical ledger events and turns
safe, structured findings into auditable proposals. It intentionally does not
directly mutate memory, scanner rules, reminders, summaries, or caregiver state.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Callable

from pydantic import BaseModel, Field

from ella.services import proposal_ingest

OBSERVER_TOOL_NAME = "ella_observer_fact_promotion"
SUPPORTED_PROPOSAL_TYPES = {
    "memory_note",
    "profile_update",
    "scanner_rule_change",
    "reminder_request",
    "summary_correction",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _compact_text(value: Any, max_chars: int = 4000) -> str:
    text = str(value or "").replace("\x00", "").strip()
    if len(text) > max_chars:
        return text[:max_chars].rstrip()
    return text


def _stable_json(value: Any) -> str:
    return json.dumps(value or {}, sort_keys=True, separators=(",", ":"), default=str)


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()[:16]


def _event_time(event: dict[str, Any]) -> str:
    return str(event.get("started_at") or event.get("created_at") or event.get("timestamp") or "")


def _cursor_from_events(events: list[dict[str, Any]]) -> str:
    if not events:
        return ""
    return max(_event_time(event) for event in events if isinstance(event, dict)) if events else ""


def _event_ref(event: dict[str, Any]) -> dict[str, Any]:
    source_ref = event.get("source_ref") if isinstance(event.get("source_ref"), dict) else {}
    return {
        "event_id": event.get("event_id"),
        "source_identity": event.get("source_identity"),
        "channel": event.get("channel"),
        "provider": event.get("provider"),
        "started_at": _event_time(event),
        "source_ref": source_ref,
    }


class ObserverCandidate(BaseModel):
    proposal_type: str
    title: str
    description: str
    requested_change: dict[str, Any] = Field(default_factory=dict)
    target: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
    confidence: float = 0.0
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class ObserverDecision(BaseModel):
    action: str
    reason: str
    event_id: str = ""
    proposal_type: str = ""
    title: str = ""
    confidence: float = 0.0
    idempotency_key: str = ""
    proposal_id: str = ""
    error: str = ""


class ObserverRunLog(BaseModel):
    run_id: str
    profile_uid: str
    canonical_identity: str = ""
    dry_run: bool = True
    status: str = "success"
    cursor_before: str = ""
    cursor_after: str = ""
    started_at: datetime
    completed_at: datetime
    latency_ms: int = 0
    source_event_count: int = 0
    source_counts: dict[str, int] = Field(default_factory=dict)
    candidate_count: int = 0
    proposal_count: int = 0
    skipped_count: int = 0
    error_count: int = 0
    proposal_ids: list[str] = Field(default_factory=list)
    decisions: list[ObserverDecision] = Field(default_factory=list)
    model_metadata: dict[str, Any] = Field(default_factory=dict)


def structured_candidate_extractor(event: dict[str, Any]) -> list[ObserverCandidate]:
    """Read structured Observer candidate proposals from event metadata.

    This is the first safe extraction boundary. Upstream Hermes/Honcho/LLM
    extraction can write candidates into event metadata; this runner validates,
    dedupes, proposes, and logs. Free-text extraction is intentionally not done
    here until it is model-backed and separately evaluated.
    """
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    observer = metadata.get("observer") if isinstance(metadata.get("observer"), dict) else {}
    raw_candidates = observer.get("proposals") or metadata.get("observer_proposals") or []
    if isinstance(raw_candidates, dict):
        raw_candidates = [raw_candidates]
    if not isinstance(raw_candidates, list):
        return []

    candidates: list[ObserverCandidate] = []
    for raw in raw_candidates:
        if not isinstance(raw, dict):
            continue
        candidate = ObserverCandidate(
            proposal_type=_compact_text(raw.get("proposal_type"), 80),
            title=_compact_text(raw.get("title"), 240),
            description=_compact_text(raw.get("description"), 4000),
            requested_change=raw.get("requested_change") if isinstance(raw.get("requested_change"), dict) else {},
            target=raw.get("target") if isinstance(raw.get("target"), dict) else {},
            reason=_compact_text(raw.get("reason"), 1000),
            confidence=float(raw.get("confidence") or 0.0),
            evidence=raw.get("evidence") if isinstance(raw.get("evidence"), list) else [],
        )
        candidates.append(candidate)
    return candidates


def _observer_claims(profile_uid: str, run_id: str) -> dict[str, Any]:
    return {
        "sub": "ella-observer-cron",
        "profile_uid": profile_uid,
        "role": "system_observer",
        "external_provider": "ella_backend",
        "grant_id": "ella-observer-cron",
        "trace_id": f"observer:{run_id}",
        "scopes": ["proposals:write"],
        "allowed_tools": [OBSERVER_TOOL_NAME],
    }


def _proposal_payload(candidate: ObserverCandidate, event: dict[str, Any], run_id: str) -> dict[str, Any]:
    evidence = list(candidate.evidence)
    evidence.append(_event_ref(event))
    return {
        "title": candidate.title,
        "description": candidate.description,
        "target": candidate.target,
        "evidence": evidence,
        "requested_change": candidate.requested_change,
        "source": "ella_observer_cron",
        "observer_run_id": run_id,
        "reason": candidate.reason,
        "confidence": candidate.confidence,
        "write_policy": "proposal_only",
    }


def run_observer(
    *,
    profile_uid: str,
    events: list[dict[str, Any]],
    canonical_identity: str = "",
    cursor_before: str = "",
    dry_run: bool = True,
    run_id: str | None = None,
    extractor: Callable[[dict[str, Any]], list[ObserverCandidate]] = structured_candidate_extractor,
    create_proposal: Callable[..., dict[str, Any]] = proposal_ingest.create_proposal,
    model_metadata: dict[str, Any] | None = None,
) -> ObserverRunLog:
    started = _now()
    monotonic_start = time.monotonic()
    actual_run_id = run_id or str(uuid.uuid4())
    decisions: list[ObserverDecision] = []
    proposal_ids: list[str] = []
    source_counts = Counter(str(event.get("channel") or "unknown") for event in events if isinstance(event, dict))

    for event in events:
        if not isinstance(event, dict):
            decisions.append(ObserverDecision(action="skip", reason="invalid_event_shape"))
            continue
        event_id = str(event.get("event_id") or "")
        try:
            candidates = extractor(event)
        except Exception as exc:
            decisions.append(
                ObserverDecision(action="error", reason="extractor_failed", event_id=event_id, error=str(exc))
            )
            continue
        if not candidates:
            decisions.append(
                ObserverDecision(action="skip", reason="no_structured_observer_candidates", event_id=event_id)
            )
            continue
        for candidate in candidates:
            if candidate.proposal_type not in SUPPORTED_PROPOSAL_TYPES:
                decisions.append(
                    ObserverDecision(
                        action="skip",
                        reason="unsupported_proposal_type",
                        event_id=event_id,
                        proposal_type=candidate.proposal_type,
                        title=candidate.title,
                        confidence=candidate.confidence,
                    )
                )
                continue
            if not candidate.title or not candidate.description:
                decisions.append(
                    ObserverDecision(
                        action="skip",
                        reason="missing_title_or_description",
                        event_id=event_id,
                        proposal_type=candidate.proposal_type,
                        title=candidate.title,
                        confidence=candidate.confidence,
                    )
                )
                continue

            payload = _proposal_payload(candidate, event, actual_run_id)
            idempotency_key = (
                f"observer:{profile_uid}:{event_id or _stable_hash(_event_ref(event))}:"
                f"{candidate.proposal_type}:{_stable_hash(payload)}"
            )
            if dry_run:
                decisions.append(
                    ObserverDecision(
                        action="would_create_proposal",
                        reason=candidate.reason or "structured_candidate_valid",
                        event_id=event_id,
                        proposal_type=candidate.proposal_type,
                        title=candidate.title,
                        confidence=candidate.confidence,
                        idempotency_key=idempotency_key,
                    )
                )
                continue
            try:
                result = create_proposal(
                    session_claims=_observer_claims(profile_uid, actual_run_id),
                    tool_name=OBSERVER_TOOL_NAME,
                    proposal_type=candidate.proposal_type,
                    payload=payload,
                    idempotency_key=idempotency_key,
                )
                proposal = result.get("proposal") or {}
                proposal_id = str(proposal.get("proposal_id") or "")
                if proposal_id:
                    proposal_ids.append(proposal_id)
                decisions.append(
                    ObserverDecision(
                        action="created_proposal" if result.get("created") else "deduped_proposal",
                        reason=candidate.reason or "structured_candidate_valid",
                        event_id=event_id,
                        proposal_type=candidate.proposal_type,
                        title=candidate.title,
                        confidence=candidate.confidence,
                        idempotency_key=idempotency_key,
                        proposal_id=proposal_id,
                    )
                )
            except Exception as exc:
                decisions.append(
                    ObserverDecision(
                        action="error",
                        reason="proposal_create_failed",
                        event_id=event_id,
                        proposal_type=candidate.proposal_type,
                        title=candidate.title,
                        confidence=candidate.confidence,
                        idempotency_key=idempotency_key,
                        error=str(exc),
                    )
                )

    completed = _now()
    error_count = sum(1 for decision in decisions if decision.action == "error")
    skipped_count = sum(1 for decision in decisions if decision.action == "skip")
    proposal_count = sum(
        1
        for decision in decisions
        if decision.action in {"would_create_proposal", "created_proposal", "deduped_proposal"}
    )
    candidate_count = proposal_count + sum(
        1
        for decision in decisions
        if decision.action == "skip"
        and decision.reason in {"unsupported_proposal_type", "missing_title_or_description"}
    )
    return ObserverRunLog(
        run_id=actual_run_id,
        profile_uid=profile_uid,
        canonical_identity=canonical_identity,
        dry_run=dry_run,
        status="error" if error_count else "success",
        cursor_before=cursor_before,
        cursor_after=_cursor_from_events(events),
        started_at=started,
        completed_at=completed,
        latency_ms=int((time.monotonic() - monotonic_start) * 1000),
        source_event_count=len(events),
        source_counts=dict(source_counts),
        candidate_count=candidate_count,
        proposal_count=proposal_count,
        skipped_count=skipped_count,
        error_count=error_count,
        proposal_ids=proposal_ids,
        decisions=decisions,
        model_metadata=model_metadata or {"extractor": "structured_candidate_extractor"},
    )


def observer_log_to_dict(log: ObserverRunLog) -> dict[str, Any]:
    if hasattr(log, "model_dump"):
        return log.model_dump(mode="json")
    return log.dict()
