"""Server-authoritative consent for third-party AI processing."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Literal, Optional, Protocol

from fastapi import Depends, Header, HTTPException
from google.cloud.firestore_v1 import transactional

from utils.other import endpoints as auth

ConsentDecision = Literal["granted", "declined", "revoked"]

CURRENT_POLICY_VERSION = "ai-data-processors-v5"
CANONICAL_PROCESSOR_SET = (
    "deepgram:stt|soniox:stt|speechmatics:stt|firebase:auth-infrastructure|"
    "hermes-self-hosted:agent-runtime|honcho-self-hosted:memory-context|ella-self-hosted-tts:tts|"
    "openrouter:model-routing|google-gemini:language-live-voice|openai:language-live-voice|"
    "groq:language|xai-grok:language-live-voice|inworld:tts|elevenlabs:tts-fallback"
)
CURRENT_PROCESSOR_SET_HASH = f"sha256:{hashlib.sha256(CANONICAL_PROCESSOR_SET.encode()).hexdigest()}"

PROCESSORS: tuple[dict[str, Any], ...] = (
    {
        "id": "deepgram",
        "legal_recipient": "Deepgram",
        "function": "Speech transcription",
        "data": "Live or stored microphone audio",
        "provider_aliases": ["deepgram", "deepgram-streaming"],
        "third_party": True,
    },
    {
        "id": "soniox",
        "legal_recipient": "Soniox",
        "function": "Speech transcription",
        "data": "Live or stored microphone audio",
        "provider_aliases": ["soniox", "soniox-streaming"],
        "third_party": True,
    },
    {
        "id": "speechmatics",
        "legal_recipient": "Speechmatics",
        "function": "Speech transcription",
        "data": "Live or stored microphone audio",
        "provider_aliases": ["speechmatics", "speechmatics-streaming"],
        "third_party": True,
    },
    {
        "id": "firebase",
        "legal_recipient": "Google Firebase",
        "function": "Authentication and service infrastructure",
        "data": "Account and service metadata",
        "provider_aliases": ["firebase", "google-firebase"],
        "third_party": True,
    },
    {
        "id": "hermes-self-hosted",
        "legal_recipient": "Ella self-hosted Hermes",
        "function": "Agent reasoning",
        "data": "Messages, transcripts, and selected memory context",
        "provider_aliases": ["hermes", "hermes-self-hosted", "hermes-retained", "hermes-isolated"],
        "third_party": False,
    },
    {
        "id": "honcho-self-hosted",
        "legal_recipient": "Ella self-hosted Honcho",
        "function": "Memory context",
        "data": "Derived text and selected memory relationships",
        "provider_aliases": ["honcho", "honcho-self-hosted"],
        "third_party": False,
    },
    {
        "id": "ella-self-hosted-tts",
        "legal_recipient": "Ella self-hosted voice synthesis",
        "function": "Voice synthesis",
        "data": "Response text",
        "provider_aliases": ["fish-audio", "fish-audio-s1", "fish-audio-s2", "kokoro"],
        "third_party": False,
    },
    {
        "id": "openrouter",
        "legal_recipient": "OpenRouter",
        "function": "Model routing",
        "data": "Messages, transcripts, and selected memory context",
        "provider_aliases": ["openrouter"],
        "third_party": True,
    },
    {
        "id": "google-gemini",
        "legal_recipient": "Google Gemini",
        "function": "Language processing and live voice",
        "data": "Text, selected context, or live microphone audio",
        "provider_aliases": ["gemini", "gemini-live", "gemini-native-live", "google-gemini"],
        "third_party": True,
    },
    {
        "id": "openai",
        "legal_recipient": "OpenAI",
        "function": "Language processing and live voice",
        "data": "Text, selected context, or live microphone audio",
        "provider_aliases": ["openai", "openai-native-realtime"],
        "third_party": True,
    },
    {
        "id": "groq",
        "legal_recipient": "Groq",
        "function": "Language processing",
        "data": "Text and selected context",
        "provider_aliases": ["groq"],
        "third_party": True,
    },
    {
        "id": "xai-grok",
        "legal_recipient": "xAI Grok",
        "function": "Language processing and live voice",
        "data": "Text, selected context, or live microphone audio",
        "provider_aliases": ["grok", "grok-voice", "xai", "xai-grok", "xai-tts"],
        "third_party": True,
    },
    {
        "id": "inworld",
        "legal_recipient": "Inworld AI",
        "function": "Voice synthesis",
        "data": "Response text",
        "provider_aliases": ["inworld"],
        "third_party": True,
    },
    {
        "id": "elevenlabs",
        "legal_recipient": "ElevenLabs",
        "function": "Fallback voice synthesis",
        "data": "Response text",
        "provider_aliases": ["elevenlabs"],
        "third_party": True,
    },
)

ACCOUNT_DELETION_CONTRACT = {
    "method": "DELETE",
    "path": "/v1/users/delete-account",
    "scope": "account_and_user_data",
}


def build_account_deletion_receipt(
    *,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, str]:
    """Return a non-identifying receipt for a completed synchronous deletion."""
    return {
        "request_id": f"aidel_{secrets.token_hex(16)}",
        "status": "completed",
        "scope": ACCOUNT_DELETION_CONTRACT["scope"],
        "server_completed_at": now().astimezone(timezone.utc).isoformat(),
    }


class ConsentPolicyMismatch(ValueError):
    pass


class ConsentIdempotencyConflict(ValueError):
    pass


class ConsentRepository(Protocol):
    def get_state(self, uid: str) -> Optional[dict[str, Any]]: ...

    def get_receipt(self, uid: str, receipt_id: str) -> Optional[dict[str, Any]]: ...

    def record(
        self,
        uid: str,
        receipt_id: str,
        receipt: dict[str, Any],
        request_fingerprint: str,
    ) -> tuple[dict[str, Any], dict[str, Any], bool]: ...


def _receipt_fingerprint(receipt: dict[str, Any]) -> str:
    material = {
        key: receipt.get(key)
        for key in (
            "decision",
            "policy_version",
            "processor_set_hash",
            "request_id",
            "app_version",
            "build_number",
            "locale",
        )
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


@transactional
def _record_firestore_receipt(
    transaction,
    user_ref,
    receipt_ref,
    receipt: dict[str, Any],
    request_fingerprint: str,
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    existing_snapshot = receipt_ref.get(transaction=transaction)
    user_snapshot = user_ref.get(transaction=transaction)
    user_data = user_snapshot.to_dict() if user_snapshot.exists else {}

    if existing_snapshot.exists:
        existing = existing_snapshot.to_dict()
        if existing.get("request_fingerprint") != request_fingerprint:
            raise ConsentIdempotencyConflict("request_id was already used with different consent metadata")
        return existing, dict(user_data.get("ai_consent") or {}), False

    state = {
        key: receipt.get(key)
        for key in (
            "receipt_id",
            "decision",
            "policy_version",
            "processor_set_hash",
            "server_decided_at",
            "app_version",
            "build_number",
            "locale",
        )
    }
    stored_receipt = {**receipt, "request_fingerprint": request_fingerprint}
    transaction.set(receipt_ref, stored_receipt)
    transaction.set(
        user_ref,
        {
            "ai_consent": state,
            # Compatibility only. The versioned receipt is the authority.
            "private_cloud_sync_enabled": receipt["decision"] == "granted",
        },
        merge=True,
    )
    return stored_receipt, state, True


class FirestoreConsentRepository:
    @staticmethod
    def _db():
        from database._client import db

        return db

    def get_state(self, uid: str) -> Optional[dict[str, Any]]:
        db = self._db()
        snapshot = db.collection("users").document(uid).get()
        if not snapshot.exists:
            return None
        return dict((snapshot.to_dict() or {}).get("ai_consent") or {}) or None

    def get_receipt(self, uid: str, receipt_id: str) -> Optional[dict[str, Any]]:
        db = self._db()
        snapshot = db.collection("users").document(uid).collection("ai_consent_receipts").document(receipt_id).get()
        return snapshot.to_dict() if snapshot.exists else None

    def record(
        self,
        uid: str,
        receipt_id: str,
        receipt: dict[str, Any],
        request_fingerprint: str,
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        db = self._db()
        user_ref = db.collection("users").document(uid)
        receipt_ref = user_ref.collection("ai_consent_receipts").document(receipt_id)
        return _record_firestore_receipt(
            db.transaction(),
            user_ref,
            receipt_ref,
            receipt,
            request_fingerprint,
        )


class InMemoryConsentRepository:
    """Test repository with the same user-scoped idempotency behavior."""

    def __init__(self) -> None:
        self.states: dict[str, dict[str, Any]] = {}
        self.receipts: dict[tuple[str, str], dict[str, Any]] = {}

    def get_state(self, uid: str) -> Optional[dict[str, Any]]:
        state = self.states.get(uid)
        return dict(state) if state else None

    def get_receipt(self, uid: str, receipt_id: str) -> Optional[dict[str, Any]]:
        receipt = self.receipts.get((uid, receipt_id))
        return dict(receipt) if receipt else None

    def record(
        self,
        uid: str,
        receipt_id: str,
        receipt: dict[str, Any],
        request_fingerprint: str,
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        key = (uid, receipt_id)
        existing = self.receipts.get(key)
        if existing:
            if existing.get("request_fingerprint") != request_fingerprint:
                raise ConsentIdempotencyConflict("request_id was already used with different consent metadata")
            return dict(existing), dict(self.states.get(uid) or {}), False

        stored = {**receipt, "request_fingerprint": request_fingerprint}
        state = {
            key: receipt.get(key)
            for key in (
                "receipt_id",
                "decision",
                "policy_version",
                "processor_set_hash",
                "server_decided_at",
                "app_version",
                "build_number",
                "locale",
            )
        }
        self.receipts[(uid, receipt_id)] = stored
        self.states[uid] = state
        return dict(stored), dict(state), True


@dataclass(frozen=True)
class ConsentSubmission:
    decision: ConsentDecision
    policy_version: str
    processor_set_hash: str
    request_id: str
    app_version: str
    build_number: str
    locale: str


class AiConsentService:
    def __init__(
        self,
        repository: ConsentRepository,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.repository = repository
        self.now = now

    @staticmethod
    def policy() -> dict[str, Any]:
        return {
            "version": CURRENT_POLICY_VERSION,
            "processor_set_hash": CURRENT_PROCESSOR_SET_HASH,
            "canonical_processor_set": CANONICAL_PROCESSOR_SET,
            "processors": [dict(processor) for processor in PROCESSORS],
        }

    def status(self, uid: str) -> dict[str, Any]:
        state = self.repository.get_state(uid)
        return _status_payload(uid, state)

    def receipt(self, uid: str, receipt_id: str) -> Optional[dict[str, Any]]:
        receipt = self.repository.get_receipt(uid, receipt_id)
        if not receipt:
            return None
        return _public_receipt(receipt)

    def submit(self, uid: str, submission: ConsentSubmission) -> dict[str, Any]:
        if submission.decision == "granted" and (
            submission.policy_version != CURRENT_POLICY_VERSION
            or submission.processor_set_hash != CURRENT_PROCESSOR_SET_HASH
        ):
            raise ConsentPolicyMismatch("grant does not match the server-required processor policy")

        receipt_id = "aicr_" + hashlib.sha256(f"{uid}:{submission.request_id}".encode()).hexdigest()[:32]
        receipt = {
            "receipt_id": receipt_id,
            "subject_uid": uid,
            "decision": submission.decision,
            "policy_version": submission.policy_version,
            "processor_set_hash": submission.processor_set_hash,
            "request_id": submission.request_id,
            "server_decided_at": self.now().astimezone(timezone.utc).isoformat(),
            "app_version": submission.app_version,
            "build_number": submission.build_number,
            "locale": submission.locale,
        }
        fingerprint = _receipt_fingerprint(receipt)
        stored_receipt, state, created = self.repository.record(uid, receipt_id, receipt, fingerprint)
        payload = _status_payload(uid, state)
        payload["receipt"] = _public_receipt(stored_receipt)
        payload["receipt_created"] = created
        return payload


def _public_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    return {
        key: receipt.get(key)
        for key in (
            "receipt_id",
            "subject_uid",
            "decision",
            "policy_version",
            "processor_set_hash",
            "server_decided_at",
            "app_version",
            "build_number",
            "locale",
        )
    }


def _is_current_grant(state: Optional[dict[str, Any]]) -> bool:
    return bool(
        state
        and state.get("decision") == "granted"
        and state.get("policy_version") == CURRENT_POLICY_VERSION
        and state.get("processor_set_hash") == CURRENT_PROCESSOR_SET_HASH
        and state.get("receipt_id")
    )


def _status_payload(uid: str, state: Optional[dict[str, Any]]) -> dict[str, Any]:
    return {
        "subject_uid": uid,
        "authorized": _is_current_grant(state),
        "enforcement_required": ai_consent_enforcement_required(uid),
        "policy": AiConsentService.policy(),
        "consent": dict(state) if state else {"decision": "not_recorded", "receipt_id": None},
        "account_deletion": {**ACCOUNT_DELETION_CONTRACT, "status": "not_requested"},
    }


_repository: ConsentRepository = FirestoreConsentRepository()


def get_ai_consent_service() -> AiConsentService:
    return AiConsentService(_repository)


def _uid_allowlist() -> set[str]:
    return {uid.strip() for uid in os.getenv("ELLA_AI_CONSENT_ENFORCEMENT_UIDS", "").split(",") if uid.strip()}


def ai_consent_enforcement_required(uid: str) -> bool:
    return os.getenv("ELLA_AI_CONSENT_ENFORCEMENT_ENABLED", "false").lower() == "true" or uid in _uid_allowlist()


def ai_consent_enforcement_active() -> bool:
    return os.getenv("ELLA_AI_CONSENT_ENFORCEMENT_ENABLED", "false").lower() == "true" or bool(_uid_allowlist())


def assert_current_ai_consent(uid: str) -> str:
    if not ai_consent_enforcement_required(uid):
        return uid
    status = get_ai_consent_service().status(uid)
    if status["authorized"]:
        return uid
    consent = status["consent"]
    raise HTTPException(
        status_code=403,
        detail={
            "code": "ai_consent_required",
            "decision": consent.get("decision", "not_recorded"),
            "required_policy_version": CURRENT_POLICY_VERSION,
            "required_processor_set_hash": CURRENT_PROCESSOR_SET_HASH,
        },
    )


def require_current_ai_consent(
    authenticated_uid: str = Depends(auth.get_current_user_uid),
) -> str:
    return assert_current_ai_consent(authenticated_uid)


def require_current_ai_consent_or_internal_tts(
    authorization: Optional[str] = Header(default=None),
    x_internal_token: Optional[str] = Header(default=None, alias="X-Ella-Internal-Token"),
    x_subject_uid: Optional[str] = Header(default=None, alias="X-Ella-Subject-Uid"),
) -> str:
    configured_internal_token = os.getenv("ELLA_INTERNAL_VOICE_TTS_TOKEN", "")
    if (
        configured_internal_token
        and x_internal_token
        and hmac.compare_digest(configured_internal_token, x_internal_token)
    ):
        subject_uid = (x_subject_uid or "").strip()
        if not subject_uid:
            raise HTTPException(status_code=400, detail={"code": "ai_consent_subject_required"})
        return assert_current_ai_consent(subject_uid)
    if authorization:
        return assert_current_ai_consent(auth.get_current_user_uid(authorization))
    if ai_consent_enforcement_active():
        raise HTTPException(status_code=401, detail={"code": "authorization_required"})
    return "migration-bypass"


def resolve_processor(provider_alias: str) -> Optional[dict[str, Any]]:
    normalized = provider_alias.strip().lower()
    for processor in PROCESSORS:
        if normalized in processor["provider_aliases"]:
            return dict(processor)
    return None
