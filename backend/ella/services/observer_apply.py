"""Safe Observer proposal application.

This applier deliberately handles only low-risk memory/profile proposals. It
does not edit scanner files, reminders, summaries, caregiver routing, or any
external delivery surface.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from database import proposals as proposals_db
from ella.routers.canonical_events import CanonicalEventIn, CanonicalEventStore
from models.proposals import Proposal, ProposalStatus

AUTO_APPLY_TYPES = {"memory_note", "profile_update"}
OBSERVER_SOURCE = "ella_observer_cron"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _proposal_type(proposal: Proposal) -> str:
    value = proposal.proposal_type
    return getattr(value, "value", str(value))


def _payload(proposal: Proposal) -> dict[str, Any]:
    return proposal.payload if isinstance(proposal.payload, dict) else {}


def _confidence(proposal: Proposal) -> float:
    try:
        return float(_payload(proposal).get("confidence") or 0.0)
    except Exception:
        return 0.0


def _observer_owned(proposal: Proposal) -> bool:
    payload = _payload(proposal)
    return payload.get("source") == OBSERVER_SOURCE or proposal.tool_name == "ella_observer_fact_promotion"


def _event_text(proposal: Proposal) -> str:
    payload = _payload(proposal)
    requested_change = payload.get("requested_change") if isinstance(payload.get("requested_change"), dict) else {}
    memory = requested_change.get("memory") or requested_change.get("profile_update") or requested_change
    title = str(payload.get("title") or "").strip()
    description = str(payload.get("description") or "").strip()
    return "\n".join(part for part in [title, description, f"Requested change: {memory}"] if part)[:6000]


def _canonical_identity(proposal: Proposal) -> str:
    payload = _payload(proposal)
    target = payload.get("target") if isinstance(payload.get("target"), dict) else {}
    return str(target.get("canonical_identity") or proposal.session_claims.get("canonical_identity") or "")


def _decision(action: str, proposal: Proposal, reason: str = "", error: str = "") -> dict[str, Any]:
    return {
        "action": action,
        "proposal_id": proposal.proposal_id,
        "proposal_type": _proposal_type(proposal),
        "title": str(_payload(proposal).get("title") or ""),
        "confidence": _confidence(proposal),
        "reason": reason,
        "error": error,
    }


async def apply_pending_observer_memory_proposals(
    *,
    profile_uid: str,
    event_store: CanonicalEventStore,
    dry_run: bool = True,
    limit: int = 20,
    min_confidence: float = 0.9,
    proposal_types: set[str] | None = None,
) -> dict[str, Any]:
    allowed_types = proposal_types or AUTO_APPLY_TYPES
    proposals = proposals_db.list_proposals(profile_uid, status=ProposalStatus.submitted.value, limit=limit)
    decisions: list[dict[str, Any]] = []
    applied_event_ids: list[str] = []

    for proposal in proposals:
        proposal_type = _proposal_type(proposal)
        if not _observer_owned(proposal):
            decisions.append(_decision("skip", proposal, "not_observer_owned"))
            continue
        if proposal_type not in allowed_types:
            decisions.append(_decision("skip", proposal, "unsupported_auto_apply_type"))
            continue
        confidence = _confidence(proposal)
        if confidence < min_confidence:
            decisions.append(_decision("skip", proposal, "below_min_confidence"))
            continue

        event_id = f"observer:proposal:{proposal.proposal_id}:applied"
        if dry_run:
            decisions.append(_decision("would_apply", proposal, "safe_memory_event"))
            continue

        try:
            await event_store.write_batch(
                [
                    CanonicalEventIn(
                        uid=profile_uid,
                        canonical_identity=_canonical_identity(proposal),
                        event_id=event_id,
                        channel="observer_memory",
                        provider="ella-observer",
                        role="system",
                        text=_event_text(proposal),
                        started_at=_now(),
                        scan_policy="none",
                        source_ref={
                            "proposal_id": proposal.proposal_id,
                            "source_identity": f"ella-observer:proposal:{proposal.proposal_id}",
                        },
                        metadata={
                            "adapter": "observer-memory-applier",
                            "proposal_id": proposal.proposal_id,
                            "proposal_type": proposal_type,
                            "confidence": confidence,
                            "source_run_id": _payload(proposal).get("observer_run_id"),
                            "requested_change": _payload(proposal).get("requested_change") or {},
                            "evidence": _payload(proposal).get("evidence") or [],
                        },
                    )
                ]
            )
            proposals_db.update_proposal_status(
                profile_uid,
                proposal.proposal_id,
                status=ProposalStatus.applied.value,
                event={
                    "stage": "applied",
                    "status": "ok",
                    "actor": "ella-observer-memory-applier",
                    "trace_id": proposal.trace_id,
                    "detail": {"canonical_event_id": event_id, "channel": "observer_memory"},
                },
            )
            applied_event_ids.append(event_id)
            decisions.append(_decision("applied", proposal, "safe_memory_event"))
        except Exception as exc:
            proposals_db.update_proposal_status(
                profile_uid,
                proposal.proposal_id,
                status=ProposalStatus.apply_failed.value,
                event={
                    "stage": "apply_failed",
                    "status": "error",
                    "actor": "ella-observer-memory-applier",
                    "trace_id": proposal.trace_id,
                    "detail": {"error": str(exc)[:500]},
                },
            )
            decisions.append(_decision("error", proposal, "apply_failed", str(exc)[:500]))

    return {
        "ok": True,
        "profile_uid": profile_uid,
        "dry_run": dry_run,
        "proposal_count": len(proposals),
        "applied_count": sum(1 for decision in decisions if decision["action"] in {"would_apply", "applied"}),
        "error_count": sum(1 for decision in decisions if decision["action"] == "error"),
        "applied_event_ids": applied_event_ids,
        "decisions": decisions,
    }
