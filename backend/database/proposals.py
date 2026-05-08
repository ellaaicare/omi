from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from google.cloud import firestore
from google.cloud.firestore_v1 import FieldFilter

from database._client import db
from models.proposals import Proposal, ProposalStatus, proposal_from_dict, proposal_to_dict

users_collection = "users"
proposals_collection = "proposals"


def _collection(profile_uid: str):
    return db.collection(users_collection).document(profile_uid).collection(proposals_collection)


def save_proposal(proposal: Proposal) -> Proposal:
    data = proposal_to_dict(proposal)
    data["updated_at"] = datetime.now(timezone.utc)
    _collection(proposal.profile_uid).document(proposal.proposal_id).set(data)
    return proposal_from_dict(data)


def get_proposal(profile_uid: str, proposal_id: str) -> Optional[Proposal]:
    if not profile_uid or not proposal_id:
        return None
    doc = _collection(profile_uid).document(proposal_id).get()
    if not getattr(doc, "exists", False):
        return None
    data = doc.to_dict() or {}
    data.setdefault("proposal_id", doc.id)
    return proposal_from_dict(data)


def get_proposal_by_idempotency_key(profile_uid: str, idempotency_key: str) -> Optional[Proposal]:
    if not profile_uid or not idempotency_key:
        return None
    docs = (
        _collection(profile_uid).where(filter=FieldFilter("idempotency_key", "==", idempotency_key)).limit(1).stream()
    )
    for doc in docs:
        data = doc.to_dict() or {}
        data.setdefault("proposal_id", doc.id)
        return proposal_from_dict(data)
    return None


def list_proposals(profile_uid: str, status: str = "", limit: int = 20) -> list[Proposal]:
    if not profile_uid:
        return []
    query = _collection(profile_uid)
    if status:
        query = query.where(filter=FieldFilter("status", "==", status))
    query = query.order_by("created_at", direction=firestore.Query.DESCENDING).limit(max(1, min(int(limit), 100)))
    return [proposal for doc in query.stream() if (proposal := proposal_from_dict(doc.to_dict() or {})) is not None]


def update_proposal_status(
    profile_uid: str,
    proposal_id: str,
    *,
    status: ProposalStatus | str,
    event: dict | None = None,
) -> Optional[Proposal]:
    proposal = get_proposal(profile_uid, proposal_id)
    if proposal is None:
        return None
    proposal.status = ProposalStatus(status)
    proposal.updated_at = datetime.now(timezone.utc)
    if event:
        proposal.events.append(event)
    return save_proposal(proposal)
