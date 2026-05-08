from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class ProposalStatus(str, Enum):
    submitted = "submitted"
    pending_review = "pending_review"
    approved = "approved"
    rejected = "rejected"
    superseded = "superseded"
    applied = "applied"
    apply_failed = "apply_failed"


class ProposalType(str, Enum):
    note = "note"
    correction = "correction"
    preference = "preference"
    rule_change = "rule_change"
    external_context = "external_context"


class ProposalEvent(BaseModel):
    stage: str
    status: str = "ok"
    at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    trace_id: str = ""
    actor: Optional[str] = None
    detail: dict[str, Any] = Field(default_factory=dict)


class Proposal(BaseModel):
    proposal_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    idempotency_key: str = ""

    # Source attribution. This intentionally stores the complete session claims
    # emitted by the MCP identity resolver so auth/audit logic cannot drift.
    session_claims: dict[str, Any] = Field(default_factory=dict)
    profile_uid: str
    source_role: str = ""
    source_provider: str = ""
    grant_id: str = ""
    trace_id: str = ""

    tool_name: str
    proposal_type: ProposalType
    payload: dict[str, Any] = Field(default_factory=dict)

    status: ProposalStatus = ProposalStatus.submitted
    review_decision: Optional[str] = None
    reviewer: Optional[str] = None
    review_note: Optional[str] = None
    applied_at: Optional[datetime] = None
    applied_target: Optional[str] = None

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    events: list[ProposalEvent] = Field(default_factory=list)

    @classmethod
    def from_claims(
        cls,
        *,
        session_claims: dict[str, Any],
        tool_name: str,
        proposal_type: ProposalType | str,
        payload: dict[str, Any],
        idempotency_key: str = "",
        proposal_id: str = "",
    ) -> "Proposal":
        profile_uid = str(session_claims.get("profile_uid") or "")
        if not profile_uid:
            raise ValueError("session_claims.profile_uid is required")
        generated_id = proposal_id or str(uuid.uuid4())
        trace_id = str(session_claims.get("trace_id") or f"proposal:{generated_id}")
        event = ProposalEvent(
            stage=ProposalStatus.submitted.value,
            status="ok",
            trace_id=trace_id,
            actor=str(session_claims.get("sub") or ""),
            detail={"tool_name": tool_name, "proposal_type": str(proposal_type)},
        )
        return cls(
            proposal_id=generated_id,
            idempotency_key=idempotency_key,
            session_claims=dict(session_claims),
            profile_uid=profile_uid,
            source_role=str(session_claims.get("role") or ""),
            source_provider=str(session_claims.get("external_provider") or ""),
            grant_id=str(session_claims.get("grant_id") or ""),
            trace_id=trace_id,
            tool_name=tool_name,
            proposal_type=proposal_type,
            payload=dict(payload),
            events=[event],
        )


def proposal_to_dict(proposal: Proposal) -> dict[str, Any]:
    if hasattr(proposal, "model_dump"):
        return proposal.model_dump(mode="python")
    return proposal.dict()


def proposal_from_dict(data: dict[str, Any] | None) -> Proposal | None:
    if not data:
        return None
    return Proposal(**data)
