"""Firestore persistence for the owner-scoped memory-artwork lifecycle."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from google.cloud import firestore
from google.cloud.firestore_v1 import transactional

from ._client import db

ARTWORK_FIELD = "artwork"
PREFERENCES_FIELD = "memory_artwork_preferences"


def _conversation_ref(uid: str, memory_id: str):
    return db.collection("users").document(uid).collection("conversations").document(memory_id)


def get_preferences(uid: str) -> dict[str, Any]:
    snapshot = db.collection("users").document(uid).get()
    if not snapshot.exists:
        return {}
    payload = snapshot.to_dict() or {}
    preferences = payload.get(PREFERENCES_FIELD)
    return dict(preferences) if isinstance(preferences, dict) else {}


def set_preferences(uid: str, preferences: dict[str, Any]) -> None:
    db.collection("users").document(uid).set({PREFERENCES_FIELD: preferences}, merge=True)


def get_conversation(uid: str, memory_id: str) -> Optional[dict[str, Any]]:
    snapshot = _conversation_ref(uid, memory_id).get()
    return snapshot.to_dict() if snapshot.exists else None


def list_recent_conversations(uid: str, *, limit: int) -> list[dict[str, Any]]:
    query = (
        db.collection("users")
        .document(uid)
        .collection("conversations")
        .order_by("created_at", direction=firestore.Query.DESCENDING)
        .limit(limit)
    )
    return [snapshot.to_dict() for snapshot in query.stream()]


def _terminal_enrichment_matches(conversation: dict[str, Any], enrichment_revision: str) -> bool:
    enrichment = conversation.get("enrichment_state") or {}
    return bool(
        not conversation.get("deletion_pending")
        and conversation.get("status") == "completed"
        and conversation.get("active_summary_version_id") == enrichment_revision
        and isinstance(enrichment, dict)
        and enrichment.get("status") == "writeback_applied"
        and enrichment.get("kind")
        in {"observer_enriched", "corrected_enriched", "hermes_enriched", "recovered_enriched"}
    )


def _reserve_generation_transaction(
    transaction,
    conversation_ref,
    *,
    enrichment_revision: str,
    generation_key: str,
    artwork_state: dict[str, Any],
) -> dict[str, Any]:
    snapshot = conversation_ref.get(transaction=transaction)
    if not snapshot.exists:
        return {"outcome": "not_found"}
    conversation = snapshot.to_dict() or {}
    if not _terminal_enrichment_matches(conversation, enrichment_revision):
        return {"outcome": "source_changed"}
    current = conversation.get(ARTWORK_FIELD) or {}
    if isinstance(current, dict) and current.get("generation_key") == generation_key:
        if current.get("status") in {"generating", "ready"}:
            return {"outcome": "existing", "artwork": dict(current)}
    transaction.update(conversation_ref, {ARTWORK_FIELD: artwork_state})
    return {"outcome": "reserved", "artwork": dict(artwork_state)}


@transactional
def _reserve_generation(transaction, conversation_ref, **kwargs):
    return _reserve_generation_transaction(transaction, conversation_ref, **kwargs)


def reserve_generation(
    uid: str,
    memory_id: str,
    *,
    enrichment_revision: str,
    generation_key: str,
    artwork_state: dict[str, Any],
) -> dict[str, Any]:
    return _reserve_generation(
        db.transaction(),
        _conversation_ref(uid, memory_id),
        enrichment_revision=enrichment_revision,
        generation_key=generation_key,
        artwork_state=artwork_state,
    )


def _claim_generation_transaction(
    transaction,
    conversation_ref,
    *,
    generation_key: str,
    lease_token: str,
    now: datetime,
    lease_seconds: int,
) -> Optional[dict[str, Any]]:
    snapshot = conversation_ref.get(transaction=transaction)
    if not snapshot.exists:
        return None
    conversation = snapshot.to_dict() or {}
    artwork = conversation.get(ARTWORK_FIELD) or {}
    if not isinstance(artwork, dict) or artwork.get("generation_key") != generation_key:
        return None
    if artwork.get("status") != "generating":
        return None
    current_expiry = artwork.get("lease_expires_at")
    if isinstance(current_expiry, datetime) and current_expiry > now:
        return None
    claimed = dict(artwork)
    claimed.update(
        {
            "lease_token": lease_token,
            "lease_expires_at": now + timedelta(seconds=max(1, lease_seconds)),
            "updated_at": now,
        }
    )
    transaction.update(conversation_ref, {ARTWORK_FIELD: claimed})
    return claimed


@transactional
def _claim_generation(transaction, conversation_ref, **kwargs):
    return _claim_generation_transaction(transaction, conversation_ref, **kwargs)


def claim_generation(
    uid: str,
    memory_id: str,
    *,
    generation_key: str,
    lease_token: str,
    now: datetime,
    lease_seconds: int = 120,
) -> Optional[dict[str, Any]]:
    return _claim_generation(
        db.transaction(),
        _conversation_ref(uid, memory_id),
        generation_key=generation_key,
        lease_token=lease_token,
        now=now,
        lease_seconds=lease_seconds,
    )


def _generation_is_current(
    conversation: dict[str, Any],
    generation_key: str,
    authority_digest: str,
    lease_token: str,
) -> bool:
    artwork = conversation.get(ARTWORK_FIELD) or {}
    return bool(
        isinstance(artwork, dict)
        and artwork.get("generation_key") == generation_key
        and artwork.get("authority_digest") == authority_digest
        and artwork.get("lease_token") == lease_token
        and artwork.get("status") == "generating"
        and _terminal_enrichment_matches(conversation, str(artwork.get("enrichment_revision") or ""))
    )


def _finalize_generation_transaction(
    transaction,
    conversation_ref,
    *,
    generation_key: str,
    authority_digest: str,
    lease_token: str,
    ready_state: dict[str, Any],
) -> bool:
    snapshot = conversation_ref.get(transaction=transaction)
    if not snapshot.exists:
        return False
    conversation = snapshot.to_dict() or {}
    if not _generation_is_current(conversation, generation_key, authority_digest, lease_token):
        return False
    transaction.update(conversation_ref, {ARTWORK_FIELD: ready_state})
    return True


@transactional
def _finalize_generation(transaction, conversation_ref, **kwargs):
    return _finalize_generation_transaction(transaction, conversation_ref, **kwargs)


def finalize_generation(
    uid: str,
    memory_id: str,
    *,
    generation_key: str,
    authority_digest: str,
    lease_token: str,
    ready_state: dict[str, Any],
) -> bool:
    return _finalize_generation(
        db.transaction(),
        _conversation_ref(uid, memory_id),
        generation_key=generation_key,
        authority_digest=authority_digest,
        lease_token=lease_token,
        ready_state=ready_state,
    )


def _mark_generation_unavailable_transaction(
    transaction,
    conversation_ref,
    *,
    generation_key: str,
    failure_code: str,
    lease_token: Optional[str] = None,
) -> bool:
    snapshot = conversation_ref.get(transaction=transaction)
    if not snapshot.exists:
        return False
    conversation = snapshot.to_dict() or {}
    current = conversation.get(ARTWORK_FIELD) or {}
    if not isinstance(current, dict) or current.get("generation_key") != generation_key:
        return False
    if lease_token is not None and current.get("lease_token") != lease_token:
        return False
    unavailable = dict(current)
    unavailable.update(
        {
            "status": "unavailable",
            "failure_code": failure_code,
            "updated_at": datetime.now(timezone.utc),
        }
    )
    unavailable.pop("lease_token", None)
    unavailable.pop("lease_expires_at", None)
    transaction.update(conversation_ref, {ARTWORK_FIELD: unavailable})
    return True


@transactional
def _mark_generation_unavailable(transaction, conversation_ref, **kwargs):
    return _mark_generation_unavailable_transaction(transaction, conversation_ref, **kwargs)


def mark_generation_unavailable(
    uid: str,
    memory_id: str,
    *,
    generation_key: str,
    failure_code: str,
    lease_token: Optional[str] = None,
) -> bool:
    return _mark_generation_unavailable(
        db.transaction(),
        _conversation_ref(uid, memory_id),
        generation_key=generation_key,
        failure_code=failure_code,
        lease_token=lease_token,
    )


def _claim_deletion_transaction(transaction, conversation_ref) -> Optional[dict[str, Any]]:
    snapshot = conversation_ref.get(transaction=transaction)
    if not snapshot.exists:
        return None
    conversation = snapshot.to_dict() or {}
    if not conversation.get("deletion_pending"):
        transaction.update(conversation_ref, {"deletion_pending": True})
        conversation["deletion_pending"] = True
    return conversation


@transactional
def _claim_deletion(transaction, conversation_ref):
    return _claim_deletion_transaction(transaction, conversation_ref)


def claim_deletion(uid: str, memory_id: str) -> Optional[dict[str, Any]]:
    return _claim_deletion(db.transaction(), _conversation_ref(uid, memory_id))
