"""One-shot, owner-bound spoken diagnostic admission for live capture."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from google.cloud.firestore_v1 import transactional


def _capture_protocol():
    from utils.conversations import capture_protocol
    return capture_protocol


def _database():
    from database._client import db
    return db


def _authority_ref(uid: str):
    return _capture_protocol()._authority_ref(uid)


def _conversation_ref(uid: str, conversation_id: str):
    return _capture_protocol()._conversation_ref(uid, conversation_id)

PHRASE = "silver lantern check in"
RESPONSE = "Spoken check-in is working."
RESPONSE_VERSION = "ella.spoken_diagnostic.v1"
MAX_WINDOW = timedelta(minutes=10)
CLAIM_TTL = timedelta(seconds=60)
_RUN_ID = re.compile(r"[a-z0-9][a-z0-9-]{5,63}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class DiagnosticConfig:
    owner_sha256: str
    run_id: str
    starts_at: datetime
    ends_at: datetime


def _aware(value: Any) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _parse_utc(value: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        return None
    return parsed.astimezone(timezone.utc)


def current_config() -> Optional[DiagnosticConfig]:
    if os.getenv("ELLA_SPOKEN_DIAGNOSTIC_ENABLED", "").strip().lower() != "true":
        return None
    owner_sha256 = os.getenv("ELLA_SPOKEN_DIAGNOSTIC_OWNER_SHA256", "").strip()
    run_id = os.getenv("ELLA_SPOKEN_DIAGNOSTIC_RUN_ID", "").strip()
    starts_at = _parse_utc(os.getenv("ELLA_SPOKEN_DIAGNOSTIC_START_UTC", ""))
    ends_at = _parse_utc(os.getenv("ELLA_SPOKEN_DIAGNOSTIC_END_UTC", ""))
    if (
        not _SHA256.fullmatch(owner_sha256)
        or not _RUN_ID.fullmatch(run_id)
        or starts_at is None
        or ends_at is None
        or ends_at <= starts_at
        or ends_at - starts_at > MAX_WINDOW
    ):
        return None
    return DiagnosticConfig(owner_sha256, run_id, starts_at, ends_at)


def _config_for(uid: str, now: datetime) -> Optional[DiagnosticConfig]:
    config = current_config()
    if not config or not config.starts_at <= now < config.ends_at:
        return None
    if hashlib.sha256(uid.encode("utf-8")).hexdigest() != config.owner_sha256:
        return None
    return config


def _normalized(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _claim_id(uid: str, run_id: str) -> str:
    return hashlib.sha256(f"{RESPONSE_VERSION}\0{uid}\0{run_id}".encode()).hexdigest()


def _queue_id(claim_id: str) -> str:
    return f"diagnostic_{claim_id[:32]}"


def _claim_ref(uid: str, claim_id: str):
    return _authority_ref(uid).collection("spoken_diagnostic_claims").document(claim_id)


def _live_tuple(authority: dict, conversation: dict, conversation_id: str, now: datetime) -> bool:
    generation = str(authority.get("generation") or "")
    owner_token = str(authority.get("owner_token") or "")
    return bool(
        authority.get("state") == "active"
        and generation
        and owner_token
        and _capture_protocol()._authority_tuple_matches(authority, conversation_id, generation, owner_token)
        and _capture_protocol()._conversation_tuple_matches(conversation, conversation_id, generation, owner_token)
        and conversation.get("capture_protocol_version") == _capture_protocol().CAPTURE_PROTOCOL_VERSION
        and conversation.get("capture_state") == "active"
        and conversation.get("status") == "in_progress"
        and str(conversation.get("capture_owner_id") or "") == owner_token
        and (_aware(authority.get("lease_expires_at")) or now) > now
        and (_aware(conversation.get("capture_lease_expires_at")) or now) > now
    )


def _claim_live(claim: dict, authority: dict, conversation: dict, now: datetime) -> bool:
    conversation_id = str(claim.get("conversation_id") or "")
    return bool(
        conversation_id
        and claim.get("response_version") == RESPONSE_VERSION
        and claim.get("generation") == authority.get("generation")
        and claim.get("owner_token") == authority.get("owner_token")
        and (_aware(claim.get("expires_at")) or now) > now
        and _live_tuple(authority, conversation, conversation_id, now)
    )


@transactional
def _reserve(transaction, uid: str, conversation_id: str, config: DiagnosticConfig, now: datetime) -> Optional[dict]:
    return _reserve_tx(transaction, uid, conversation_id, config, now)


def _reserve_tx(transaction, uid: str, conversation_id: str, config: DiagnosticConfig, now: datetime) -> Optional[dict]:
    authority_ref = _authority_ref(uid)
    conversation_ref = _conversation_ref(uid, conversation_id)
    claim_id = _claim_id(uid, config.run_id)
    claim_ref = _claim_ref(uid, claim_id)
    authority_snapshot = authority_ref.get(transaction=transaction)
    conversation_snapshot = conversation_ref.get(transaction=transaction)
    claim_snapshot = claim_ref.get(transaction=transaction)
    if claim_snapshot.exists or not authority_snapshot.exists or not conversation_snapshot.exists:
        return None
    authority = authority_snapshot.to_dict() or {}
    conversation = conversation_snapshot.to_dict() or {}
    if not _live_tuple(authority, conversation, conversation_id, now):
        return None
    expires_at = min(config.ends_at, now + CLAIM_TTL)
    claim = {
        "state": "claimed",
        "run_id": config.run_id,
        "conversation_id": conversation_id,
        "generation": authority["generation"],
        "owner_token": authority["owner_token"],
        "response_version": RESPONSE_VERSION,
        "queue_id": _queue_id(claim_id),
        "created_at": now,
        "expires_at": expires_at,
    }
    transaction.create(claim_ref, claim)
    return {"claim_id": claim_id, "queue_id": claim["queue_id"], "expires_at": expires_at.isoformat(), "response_version": RESPONSE_VERSION}


def reserve_for_segments(uid: str, conversation_id: str, segments: list[dict], *, firestore_db=None) -> Optional[dict]:
    now = datetime.now(timezone.utc)
    config = _config_for(uid, now)
    if config is None or not _phrase_in_segments(segments):
        return None
    return _reserve((firestore_db or _database()).transaction(), uid, conversation_id, config, now)


def _phrase_in_segments(segments: list[dict]) -> bool:
    normalized = [_normalized(str(segment.get("text") or "")) for segment in segments]
    normalized = [value for value in normalized if value]
    return normalized == [PHRASE]


def is_diagnostic_window(uid: str, segments: list[dict]) -> bool:
    return _config_for(uid, datetime.now(timezone.utc)) is not None and _phrase_in_segments(segments)


@transactional
def _transition(transaction, uid: str, claim_id: str, expected: str, next_state: str, now: datetime, update: Optional[dict] = None) -> Optional[dict]:
    return _transition_tx(transaction, uid, claim_id, expected, next_state, now, update)


def _transition_tx(transaction, uid: str, claim_id: str, expected: str, next_state: str, now: datetime, update: Optional[dict] = None) -> Optional[dict]:
    authority_ref = _authority_ref(uid)
    claim_ref = _claim_ref(uid, claim_id)
    authority_snapshot = authority_ref.get(transaction=transaction)
    claim_snapshot = claim_ref.get(transaction=transaction)
    if not authority_snapshot.exists or not claim_snapshot.exists:
        return None
    authority = authority_snapshot.to_dict() or {}
    claim = claim_snapshot.to_dict() or {}
    conversation_id = str(claim.get("conversation_id") or "")
    if not conversation_id:
        return None
    conversation_snapshot = _conversation_ref(uid, conversation_id).get(transaction=transaction)
    if (
        not conversation_snapshot.exists
        or claim.get("state") != expected
        or claim_id != _claim_id(uid, str(claim.get("run_id") or ""))
        or not _claim_live(claim, authority, conversation_snapshot.to_dict() or {}, now)
    ):
        return None
    values = {**(update or {}), "state": next_state, "updated_at": now}
    transaction.update(claim_ref, values)
    return {**claim, **values}


def transition(uid: str, claim_id: str, expected: str, next_state: str, *, update: Optional[dict] = None, firestore_db=None) -> Optional[dict]:
    now = datetime.now(timezone.utc)
    config = _config_for(uid, now)
    if config is None or claim_id != _claim_id(uid, config.run_id):
        return None
    return _transition((firestore_db or _database()).transaction(), uid, claim_id, expected, next_state, now, update)


def current_claim(uid: str, claim_id: str, required_state: str, *, firestore_db=None) -> Optional[dict]:
    now = datetime.now(timezone.utc)
    config = _config_for(uid, now)
    if config is None or claim_id != _claim_id(uid, config.run_id):
        return None
    return _current_claim((firestore_db or _database()).transaction(), uid, claim_id, required_state, now)


@transactional
def _current_claim(transaction, uid: str, claim_id: str, required_state: str, now: datetime) -> Optional[dict]:
    return _current_claim_tx(transaction, uid, claim_id, required_state, now)


def _current_claim_tx(transaction, uid: str, claim_id: str, required_state: str, now: datetime) -> Optional[dict]:
    claim_snapshot = _claim_ref(uid, claim_id).get(transaction=transaction)
    authority_snapshot = _authority_ref(uid).get(transaction=transaction)
    if not claim_snapshot.exists or not authority_snapshot.exists:
        return None
    claim = claim_snapshot.to_dict() or {}
    if claim.get("state") != required_state:
        return None
    authority = authority_snapshot.to_dict() or {}
    conversation_id = str(claim.get("conversation_id") or "")
    conversation_snapshot = _conversation_ref(uid, conversation_id).get(transaction=transaction) if conversation_id else None
    if not conversation_snapshot or not conversation_snapshot.exists:
        return None
    return claim if _claim_live(claim, authority, conversation_snapshot.to_dict() or {}, now) else None
