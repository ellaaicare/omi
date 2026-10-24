"""Proposal-only MCP writeback scaffold.

External MCP clients must not directly mutate memory, scanner rules, summaries,
or caregiver flows. This service creates auditable proposal records that later
observer/review code can approve or reject.
"""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from database import proposals as proposals_db
from models.proposals import Proposal, ProposalType, proposal_to_dict

MAX_TEXT_CHARS = 4000
PROMPT_INJECTION_MARKERS = re.compile(
    r"(?i)(ignore\s+previous\s+instructions|system\s*prompt|developer\s*message|jailbreak|do\s+anything\s+now)"
)


class ProposalIngestError(Exception):
    pass


class ProposalPermissionError(ProposalIngestError):
    pass


def _clean_text(value: str) -> str:
    text = value.replace("\x00", "").strip()
    text = PROMPT_INJECTION_MARKERS.sub("[filtered]", text)
    if len(text) > MAX_TEXT_CHARS:
        return text[:MAX_TEXT_CHARS].rstrip()
    return text


def sanitize_payload(value: Any) -> Any:
    if isinstance(value, str):
        return _clean_text(value)
    if isinstance(value, list):
        return [sanitize_payload(item) for item in value[:100]]
    if isinstance(value, dict):
        return {str(key)[:120]: sanitize_payload(item) for key, item in value.items()}
    return deepcopy(value)


def _profile_uid(session_claims: dict[str, Any]) -> str:
    uid = str(session_claims.get("profile_uid") or "")
    if not uid:
        raise ProposalPermissionError("session_claims.profile_uid is required")
    return uid


def check_tool_allowed(session_claims: dict[str, Any], tool_name: str) -> None:
    allowed = [str(tool) for tool in session_claims.get("allowed_tools") or [] if str(tool)]
    if not allowed:
        raise ProposalPermissionError("session_claims.allowed_tools must explicitly allow proposal writes")
    if tool_name not in allowed:
        raise ProposalPermissionError(f"Tool '{tool_name}' is not allowed for this MCP session")


def check_write_scope(session_claims: dict[str, Any]) -> None:
    scopes = {str(scope) for scope in session_claims.get("scopes") or [] if str(scope)}
    if "proposals:write" not in scopes:
        raise ProposalPermissionError("scope 'proposals:write' is required to create proposals")


def create_proposal(
    *,
    session_claims: dict[str, Any],
    tool_name: str,
    proposal_type: ProposalType | str,
    payload: dict[str, Any],
    idempotency_key: str = "",
) -> dict[str, Any]:
    check_write_scope(session_claims)
    check_tool_allowed(session_claims, tool_name)
    profile_uid = _profile_uid(session_claims)
    if idempotency_key:
        existing = proposals_db.get_proposal_by_idempotency_key(profile_uid, idempotency_key)
        if existing:
            return {"created": False, "deduped": True, "proposal": proposal_to_public(existing)}

    proposal = Proposal.from_claims(
        session_claims=session_claims,
        tool_name=tool_name,
        proposal_type=proposal_type,
        payload=sanitize_payload(payload),
        idempotency_key=idempotency_key,
    )
    saved = proposals_db.save_proposal(proposal)
    return {"created": True, "deduped": False, "proposal": proposal_to_public(saved)}


def get_proposal_status(*, session_claims: dict[str, Any], proposal_id: str) -> dict[str, Any]:
    profile_uid = _profile_uid(session_claims)
    proposal = proposals_db.get_proposal(profile_uid, proposal_id)
    if proposal is None:
        raise ProposalIngestError("Proposal not found")
    return proposal_to_public(proposal)


def proposal_to_public(proposal: Proposal) -> dict[str, Any]:
    data = proposal_to_dict(proposal)
    proposal_type = data.get("proposal_type")
    status = data.get("status")
    return {
        "proposal_id": data.get("proposal_id"),
        "idempotency_key": data.get("idempotency_key") or "",
        "profile_uid": data.get("profile_uid"),
        "source_role": data.get("source_role"),
        "source_provider": data.get("source_provider"),
        "grant_id": data.get("grant_id"),
        "trace_id": data.get("trace_id"),
        "tool_name": data.get("tool_name"),
        "proposal_type": getattr(proposal_type, "value", proposal_type),
        "status": getattr(status, "value", status),
        "review_decision": data.get("review_decision"),
        "review_note": data.get("review_note"),
        "applied_at": data.get("applied_at"),
        "applied_target": data.get("applied_target"),
        "created_at": data.get("created_at"),
        "updated_at": data.get("updated_at"),
        "event_count": len(data.get("events") or []),
    }
