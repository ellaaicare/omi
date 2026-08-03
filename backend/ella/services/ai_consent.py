"""Server-authoritative consent for third-party AI processing."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Literal, Optional, Protocol

from fastapi import Depends, Header, HTTPException
from google.cloud.firestore_v1 import transactional

from utils.other import endpoints as auth

ConsentDecision = Literal["granted", "declined", "revoked"]

# V7 is immutable historical consent. Cloud memory changed from a Honcho Cloud
# processor to Hermes Cloud built-in profile-scoped memory, so v8 is a
# forward-only consent version and cannot accept stale v7 receipts.
LEGACY_POLICY_VERSION_V7 = "ai-data-processors-v7"
CURRENT_POLICY_VERSION = "ai-data-processors-v8"
CANONICAL_PROCESSOR_SET = (
    "deepgram:stt|soniox:stt|speechmatics:stt|firebase:auth-infrastructure|"
    "hermes-self-hosted:agent-runtime|honcho-self-hosted:memory-context|ella-self-hosted-tts:tts|"
    "nous-hermes-cloud:managed-agent-runtime|hermes-profile-memory:profile-scoped-memory|"
    "openai-codex:managed-agent-model|photon:messaging-delivery|"
    "openrouter:model-routing|google-gemini:language-live-voice|openai:language-live-voice|"
    "groq:language|xai-grok:language-live-voice|inworld:tts|elevenlabs:tts-fallback"
)
CURRENT_PROCESSOR_SET_HASH = f"sha256:{hashlib.sha256(CANONICAL_PROCESSOR_SET.encode()).hexdigest()}"
CURRENT_SCOPE_VERSION = "managed-cloud-internal-pilot-v2"
CANONICAL_SCOPE = (
    "profile_binding=server-profile-v1|runtime_provider=hermes_cloud|"
    "model_route=openai-codex/gpt-5.6-terra|memory_provider=hermes_profile_scoped_memory|"
    "photon_scope=shared_test_line_explicit_contact_v1;allow_all=false;caregiver=false;attachments=false"
)
CURRENT_SCOPE_HASH = f"sha256:{hashlib.sha256(CANONICAL_SCOPE.encode()).hexdigest()}"
MANAGED_CLOUD_RUNTIME_PROVIDER = "hermes_cloud"
MANAGED_CLOUD_MODEL_ROUTE = "openai-codex/gpt-5.6-terra"
MANAGED_CLOUD_MEMORY_PROVIDER = "hermes_profile_scoped_memory"
MANAGED_CLOUD_PHOTON_SCOPE = "shared_test_line_explicit_contact_v1;allow_all=false;caregiver=false;attachments=false"

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
        "id": "nous-hermes-cloud",
        "legal_recipient": "Nous Research / Hermes Cloud",
        "function": "Managed agent runtime",
        "data": ("What the person says or types, details they choose to share, " "and basic session information"),
        "provider_aliases": ["hermes-cloud", "hermes_cloud", "nous-hermes-cloud"],
        "third_party": True,
    },
    {
        "id": "hermes-profile-memory",
        "legal_recipient": "Nous Research / Hermes Cloud",
        "function": "Built-in profile-scoped memory and context inside the managed Hermes Cloud runtime",
        "data": (
            "Profile-bound conversation text, saved facts, derived memory context, and session identifiers "
            "needed to retrieve memory for the same account/profile scope"
        ),
        "provider_aliases": ["hermes-profile-memory", "hermes_profile_scoped_memory"],
        "third_party": True,
    },
    {
        "id": "openai-codex",
        "legal_recipient": "OpenAI",
        "function": "Managed agent model processing",
        "data": "Model input and output through the approved OpenAI Codex OAuth route",
        "provider_aliases": ["openai-codex", "openai-codex/gpt-5.6-terra"],
        "third_party": True,
    },
    {
        "id": "photon",
        "legal_recipient": "Photon",
        "function": "Test/shared-line message delivery",
        "data": "Message content and messaging identifiers for one explicitly allowed test contact",
        "provider_aliases": ["photon", "hermes-cloud-photon"],
        "third_party": True,
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
    status: str = "completed",
    remaining: tuple[str, ...] = (),
    external_cleanup_references: tuple[str, ...] = (),
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    """Return a non-identifying receipt for deletion progress."""
    if status not in {"completed", "pending"}:
        raise ValueError("account_deletion_receipt_status_invalid")
    allowed_remaining = {
        "firebase_identity",
        "firestore_data",
        "hermes_profile",
        "honcho_tenancy",
        "memory_reinterpretation",
        "routing_traces",
        "runtime_registry",
    }
    if any(item not in allowed_remaining for item in remaining):
        raise ValueError("account_deletion_receipt_remaining_invalid")
    if any(
        not isinstance(value, str)
        or len(value) != 25
        or not value.startswith("ella-ext-")
        or any(character not in "0123456789abcdef" for character in value[9:])
        for value in external_cleanup_references
    ):
        raise ValueError("account_deletion_receipt_external_reference_invalid")
    timestamp = now().astimezone(timezone.utc).isoformat()
    receipt: dict[str, Any] = {
        "request_id": f"aidel_{secrets.token_hex(16)}",
        "status": status,
        "scope": ACCOUNT_DELETION_CONTRACT["scope"],
    }
    if status == "pending":
        receipt["server_updated_at"] = timestamp
        receipt["remaining"] = sorted(set(remaining))
        receipt["operator_action_required"] = bool(
            {"hermes_profile", "honcho_tenancy", "runtime_registry"} & set(remaining)
        )
        if external_cleanup_references:
            receipt["external_cleanup_references"] = sorted(set(external_cleanup_references))
    else:
        receipt["server_completed_at"] = timestamp
    return receipt


class ConsentPolicyMismatch(ValueError):
    pass


class ConsentIdempotencyConflict(ValueError):
    pass


class ManagedCloudConsentError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _valid_server_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        decided_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return decided_at.tzinfo is not None


def derive_profile_binding_id(*, account_uid: str, profile_uid: str) -> str:
    """Derive an opaque, stable binding from server-owned account/profile authority."""
    account_uid = str(account_uid or "").strip()
    profile_uid = str(profile_uid or "").strip()
    if not account_uid or not profile_uid:
        raise ManagedCloudConsentError("managed_cloud_profile_binding_missing")
    digest = hashlib.sha256(
        f"ella-managed-cloud-profile-v1\x1f{account_uid}\x1f{profile_uid}".encode("utf-8")
    ).hexdigest()
    return f"aipb_{digest[:32]}"


_firestore_db: Any = None


def configure_firestore_db(firestore_db: Any) -> None:
    global _firestore_db
    _firestore_db = firestore_db


class ConsentRepository(Protocol):
    def get_state(self, uid: str) -> Optional[dict[str, Any]]: ...

    def get_receipt(self, uid: str, receipt_id: str) -> Optional[dict[str, Any]]: ...

    def get_current(
        self,
        uid: str,
    ) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]: ...

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
            "scope_version",
            "scope_hash",
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
            "processor_ids",
            "profile_binding_id",
            "scope_version",
            "scope_hash",
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


@transactional
def _read_firestore_current_receipt(
    transaction,
    user_ref,
) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    user_snapshot = user_ref.get(transaction=transaction)
    if not user_snapshot.exists:
        return None, None
    state = dict((user_snapshot.to_dict() or {}).get("ai_consent") or {}) or None
    receipt_id = str((state or {}).get("receipt_id") or "")
    if not receipt_id:
        return state, None
    receipt_snapshot = user_ref.collection("ai_consent_receipts").document(receipt_id).get(transaction=transaction)
    receipt = receipt_snapshot.to_dict() if receipt_snapshot.exists else None
    return state, receipt


class FirestoreConsentRepository:
    @staticmethod
    def _configured_db():
        if _firestore_db is None:
            raise RuntimeError("AI consent Firestore client is not configured")
        return _firestore_db

    def get_state(self, uid: str) -> Optional[dict[str, Any]]:
        db = self._configured_db()
        snapshot = db.collection("users").document(uid).get()
        if not snapshot.exists:
            return None
        return dict((snapshot.to_dict() or {}).get("ai_consent") or {}) or None

    def get_receipt(self, uid: str, receipt_id: str) -> Optional[dict[str, Any]]:
        db = self._configured_db()
        snapshot = db.collection("users").document(uid).collection("ai_consent_receipts").document(receipt_id).get()
        return snapshot.to_dict() if snapshot.exists else None

    def get_current(
        self,
        uid: str,
    ) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
        db = self._configured_db()
        user_ref = db.collection("users").document(uid)
        return _read_firestore_current_receipt(db.transaction(), user_ref)

    def record(
        self,
        uid: str,
        receipt_id: str,
        receipt: dict[str, Any],
        request_fingerprint: str,
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        db = self._configured_db()
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
        self._lock = threading.RLock()

    def get_state(self, uid: str) -> Optional[dict[str, Any]]:
        with self._lock:
            state = self.states.get(uid)
            return dict(state) if state else None

    def get_receipt(self, uid: str, receipt_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            receipt = self.receipts.get((uid, receipt_id))
            return dict(receipt) if receipt else None

    def get_current(
        self,
        uid: str,
    ) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
        with self._lock:
            state = self.states.get(uid)
            state_copy = dict(state) if state else None
            receipt_id = str((state_copy or {}).get("receipt_id") or "")
            receipt = self.receipts.get((uid, receipt_id)) if receipt_id else None
            return state_copy, dict(receipt) if receipt else None

    def record(
        self,
        uid: str,
        receipt_id: str,
        receipt: dict[str, Any],
        request_fingerprint: str,
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        with self._lock:
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
                    "processor_ids",
                    "profile_binding_id",
                    "scope_version",
                    "scope_hash",
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
    scope_version: str = ""
    scope_hash: str = ""


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
            "scope_version": CURRENT_SCOPE_VERSION,
            "scope_hash": CURRENT_SCOPE_HASH,
            "canonical_scope": CANONICAL_SCOPE,
            "processors": [dict(processor) for processor in PROCESSORS],
        }

    def status(self, uid: str) -> dict[str, Any]:
        state, receipt = self.repository.get_current(uid)
        return _status_payload(uid, state, receipt)

    def receipt(self, uid: str, receipt_id: str) -> Optional[dict[str, Any]]:
        receipt = self.repository.get_receipt(uid, receipt_id)
        if not receipt:
            return None
        return _public_receipt(receipt)

    def submit(self, uid: str, submission: ConsentSubmission) -> dict[str, Any]:
        if submission.decision == "granted" and (
            submission.policy_version != CURRENT_POLICY_VERSION
            or submission.processor_set_hash != CURRENT_PROCESSOR_SET_HASH
            or submission.scope_version != CURRENT_SCOPE_VERSION
            or submission.scope_hash != CURRENT_SCOPE_HASH
        ):
            raise ConsentPolicyMismatch("grant does not match the server-required processor policy")

        receipt_id = "aicr_" + hashlib.sha256(f"{uid}:{submission.request_id}".encode()).hexdigest()[:32]
        profile_binding_id = derive_profile_binding_id(account_uid=uid, profile_uid=uid)
        receipt = {
            "receipt_id": receipt_id,
            "subject_uid": uid,
            "decision": submission.decision,
            "policy_version": submission.policy_version,
            "processor_set_hash": submission.processor_set_hash,
            "processor_ids": [str(processor["id"]) for processor in PROCESSORS],
            "profile_binding_id": profile_binding_id,
            "scope_version": submission.scope_version,
            "scope_hash": submission.scope_hash,
            "request_id": submission.request_id,
            "server_decided_at": self.now().astimezone(timezone.utc).isoformat(),
            "app_version": submission.app_version,
            "build_number": submission.build_number,
            "locale": submission.locale,
        }
        fingerprint = _receipt_fingerprint(receipt)
        stored_receipt, state, created = self.repository.record(uid, receipt_id, receipt, fingerprint)
        payload = _status_payload(uid, state, stored_receipt)
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
            "processor_ids",
            "profile_binding_id",
            "scope_version",
            "scope_hash",
            "server_decided_at",
            "app_version",
            "build_number",
            "locale",
        )
    }


def _is_current_grant(
    uid: str,
    state: Optional[dict[str, Any]],
    receipt: Optional[dict[str, Any]],
) -> bool:
    expected_processor_ids = [str(processor["id"]) for processor in PROCESSORS]
    exact_fields = (
        "receipt_id",
        "decision",
        "policy_version",
        "processor_set_hash",
        "processor_ids",
        "profile_binding_id",
        "scope_version",
        "scope_hash",
        "server_decided_at",
        "app_version",
        "build_number",
        "locale",
    )
    return bool(
        state
        and receipt
        and receipt.get("subject_uid") == uid
        and all(receipt.get(field) == state.get(field) for field in exact_fields)
        and state.get("decision") == "granted"
        and state.get("policy_version") == CURRENT_POLICY_VERSION
        and state.get("processor_set_hash") == CURRENT_PROCESSOR_SET_HASH
        and state.get("processor_ids") == expected_processor_ids
        and state.get("profile_binding_id")
        and state.get("scope_version") == CURRENT_SCOPE_VERSION
        and state.get("scope_hash") == CURRENT_SCOPE_HASH
        and _valid_server_timestamp(state.get("server_decided_at"))
        and state.get("receipt_id")
    )


def _status_payload(
    uid: str,
    state: Optional[dict[str, Any]],
    receipt: Optional[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "subject_uid": uid,
        "authorized": _is_current_grant(uid, state, receipt),
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


def ai_consent_global_enforcement_enabled() -> bool:
    return os.getenv("ELLA_AI_CONSENT_ENFORCEMENT_ENABLED", "false").lower() == "true"


def managed_cloud_real_data_enabled(uid: str) -> bool:
    enabled = os.getenv("ELLA_MANAGED_CLOUD_REAL_DATA_ENABLED", "false").strip().lower() == "true"
    enabled_uids = {
        value.strip()
        for value in os.getenv("ELLA_MANAGED_CLOUD_REAL_DATA_ENABLED_UIDS", "").split(",")
        if value.strip()
    }
    return enabled or uid in enabled_uids


def assert_managed_cloud_consent(
    account_uid: str,
    *,
    profile_uid: str,
    runtime_provider: str,
    model_route: str,
    memory_provider: str,
    photon_scope: str,
) -> str:
    """Return the immutable receipt id authorizing one exact egress contract."""
    if not managed_cloud_real_data_enabled(account_uid):
        raise ManagedCloudConsentError("managed_cloud_real_data_disabled")
    if (
        runtime_provider != MANAGED_CLOUD_RUNTIME_PROVIDER
        or model_route != MANAGED_CLOUD_MODEL_ROUTE
        or memory_provider != MANAGED_CLOUD_MEMORY_PROVIDER
        or photon_scope != MANAGED_CLOUD_PHOTON_SCOPE
    ):
        raise ManagedCloudConsentError("managed_cloud_consent_scope_drift")

    expected_profile_binding_id = derive_profile_binding_id(
        account_uid=account_uid,
        profile_uid=profile_uid,
    )
    status = get_ai_consent_service().status(account_uid)
    state = dict(status.get("consent") or {})
    if status.get("authorized") is not True or state.get("decision") != "granted":
        raise ManagedCloudConsentError("managed_cloud_consent_required")
    if (
        state.get("policy_version") != CURRENT_POLICY_VERSION
        or state.get("processor_set_hash") != CURRENT_PROCESSOR_SET_HASH
        or state.get("scope_version") != CURRENT_SCOPE_VERSION
        or state.get("scope_hash") != CURRENT_SCOPE_HASH
        or state.get("profile_binding_id") != expected_profile_binding_id
        or not _valid_server_timestamp(state.get("server_decided_at"))
    ):
        raise ManagedCloudConsentError("managed_cloud_consent_stale")
    receipt_id = str(state.get("receipt_id") or "")
    if not receipt_id:
        raise ManagedCloudConsentError("managed_cloud_consent_stale")
    return receipt_id


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
    authenticated_uid: str = Depends(auth.get_writable_user_uid),
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
    if ai_consent_global_enforcement_enabled():
        raise HTTPException(status_code=401, detail={"code": "authorization_required"})
    # A legacy anonymous request has no trustworthy subject to compare with the
    # UID canary. Canary clients must send Firebase auth; global enforcement
    # removes this migration bridge for every caller.
    return "migration-bypass"


def resolve_processor(provider_alias: str) -> Optional[dict[str, Any]]:
    normalized = provider_alias.strip().lower()
    for processor in PROCESSORS:
        if normalized in processor["provider_aliases"]:
            return dict(processor)
    return None
