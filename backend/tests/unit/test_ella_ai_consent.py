import asyncio
import hashlib
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ella.routers import ai_consent
from ella.services import ai_consent as consent, consent_authority
from database import managed_cloud_consent
from utils.ella.exact_firebase_auth import (
    FirebaseTokenIdentity,
    get_exact_firebase_uid,
    get_firebase_token_identity,
)


def _submission(
    *,
    decision="granted",
    request_id="request-0001",
    policy_version=consent.CURRENT_POLICY_VERSION,
    processor_set_hash=consent.CURRENT_PROCESSOR_SET_HASH,
    scope_version=consent.CURRENT_SCOPE_VERSION,
    scope_hash=consent.CURRENT_SCOPE_HASH,
):
    return consent.ConsentSubmission(
        decision=decision,
        policy_version=policy_version,
        processor_set_hash=processor_set_hash,
        request_id=request_id,
        app_version="1.0.0",
        build_number="804",
        locale="en-US",
        scope_version=scope_version,
        scope_hash=scope_hash,
    )


def _service(repository=None):
    return consent.AiConsentService(
        repository or consent.InMemoryConsentRepository(),
        now=lambda: datetime(2026, 7, 26, 23, 45, tzinfo=timezone.utc),
    )


def test_policy_matches_exact_managed_cloud_v10_artwork_contract():
    policy = consent.AiConsentService.policy()

    assert policy["version"] == "ai-data-processors-v10"
    assert policy["processor_set_hash"] == consent.CURRENT_PROCESSOR_SET_HASH
    assert policy["scope_version"] == "managed-cloud-internal-pilot-v4"
    assert policy["scope_hash"] == consent.CURRENT_SCOPE_HASH
    assert (
        "|".join(
            [
                "deepgram:stt",
                "soniox:stt",
                "speechmatics:stt",
                "firebase:auth-infrastructure",
                "hermes-self-hosted:agent-runtime",
                "honcho-self-hosted:memory-context",
                "ella-self-hosted-tts:tts",
                "nous-hermes-cloud:managed-agent-runtime",
                "hermes-profile-memory:profile-scoped-memory",
                "openai-codex:managed-agent-model-memory-illustration",
                "photon:messaging-delivery",
                "openrouter:model-routing",
                "google-gemini:language-live-voice",
                "openai:language-live-voice",
                "groq:language",
                "xai-grok:language-live-voice",
                "inworld:tts",
                "elevenlabs:tts-fallback",
            ]
        )
        == policy["canonical_processor_set"]
    )
    assert consent.CURRENT_SCOPE_HASH == f"sha256:{hashlib.sha256(policy['canonical_scope'].encode()).hexdigest()}"
    assert (
        "artwork_provider=openai-codex/gpt-image-2-medium;reasoning_host=openai-codex/gpt-5.6-luna;"
        "source=selected_memory_summary_only;raw_audio=false;source_photos=false"
    ) in policy["canonical_scope"]
    processors = {processor["id"]: processor for processor in policy["processors"]}
    assert processors["nous-hermes-cloud"] == {
        "id": "nous-hermes-cloud",
        "legal_recipient": "Nous Research / Hermes Cloud",
        "function": "Managed agent runtime",
        "data": ("What the person says or types, details they choose to share, " "and basic session information"),
        "provider_aliases": [
            "hermes-cloud",
            "hermes_cloud",
            "nous-hermes-cloud",
        ],
        "third_party": True,
    }
    assert "xai-imagine" not in processors
    assert "honcho-cloud" not in processors
    assert processors["hermes-profile-memory"] == {
        "id": "hermes-profile-memory",
        "legal_recipient": "Nous Research / Hermes Cloud",
        "function": "Built-in profile-scoped memory and context inside the managed Hermes Cloud runtime",
        "data": (
            "Profile-bound conversation text, saved facts, derived memory context, and session identifiers "
            "needed to retrieve memory for the same account/profile scope"
        ),
        "provider_aliases": [
            "hermes-profile-memory",
            "hermes_profile_scoped_memory",
        ],
        "third_party": True,
    }
    assert [
        (
            processor["id"],
            processor["legal_recipient"],
            processor["function"],
            processor["data"],
            processor["third_party"],
        )
        for processor in policy["processors"]
    ] == [
        ("deepgram", "Deepgram", "Speech transcription", "Live or stored microphone audio", True),
        ("soniox", "Soniox", "Speech transcription", "Live or stored microphone audio", True),
        (
            "speechmatics",
            "Speechmatics",
            "Speech transcription",
            "Live or stored microphone audio",
            True,
        ),
        (
            "firebase",
            "Google Firebase",
            "Authentication and service infrastructure",
            "Account and service metadata",
            True,
        ),
        (
            "hermes-self-hosted",
            "Ella self-hosted Hermes",
            "Agent reasoning",
            "Messages, transcripts, and selected memory context",
            False,
        ),
        (
            "honcho-self-hosted",
            "Ella self-hosted Honcho",
            "Memory context",
            "Derived text and selected memory relationships",
            False,
        ),
        (
            "ella-self-hosted-tts",
            "Ella self-hosted voice synthesis",
            "Voice synthesis",
            "Response text",
            False,
        ),
        (
            "nous-hermes-cloud",
            "Nous Research / Hermes Cloud",
            "Managed agent runtime",
            "What the person says or types, details they choose to share, and basic session information",
            True,
        ),
        (
            "hermes-profile-memory",
            "Nous Research / Hermes Cloud",
            "Built-in profile-scoped memory and context inside the managed Hermes Cloud runtime",
            (
                "Profile-bound conversation text, saved facts, derived memory context, and session identifiers "
                "needed to retrieve memory for the same account/profile scope"
            ),
            True,
        ),
        (
            "openai-codex",
            "OpenAI",
            "Managed agent processing and saved-memory illustration",
            (
                "Model input and output, plus a selected memory title and summary for an illustration, through the "
                "approved OpenAI Codex OAuth route; no raw microphone audio or source photos for artwork"
            ),
            True,
        ),
        (
            "photon",
            "Photon",
            "Test/shared-line message delivery",
            "Message content and messaging identifiers for one explicitly allowed test contact",
            True,
        ),
        (
            "openrouter",
            "OpenRouter",
            "Model routing",
            "Messages, transcripts, and selected memory context",
            True,
        ),
        (
            "google-gemini",
            "Google Gemini",
            "Language processing and live voice",
            "Text, selected context, or live microphone audio",
            True,
        ),
        (
            "openai",
            "OpenAI",
            "Language processing and live voice",
            "Text, selected context, or live microphone audio",
            True,
        ),
        ("groq", "Groq", "Language processing", "Text and selected context", True),
        (
            "xai-grok",
            "xAI Grok",
            "Language processing and live voice",
            "Text, selected context, or live microphone audio",
            True,
        ),
        ("inworld", "Inworld AI", "Voice synthesis", "Response text", True),
        (
            "elevenlabs",
            "ElevenLabs",
            "Fallback voice synthesis",
            "Response text",
            True,
        ),
    ]


def test_missing_consent_is_fail_closed_when_enforcement_is_enabled(monkeypatch):
    repository = consent.InMemoryConsentRepository()
    monkeypatch.setattr(consent, "_repository", repository)
    monkeypatch.setenv("ELLA_AI_CONSENT_ENFORCEMENT_ENABLED", "true")

    with pytest.raises(HTTPException) as error:
        consent.assert_current_ai_consent("user-a")

    assert error.value.status_code == 403
    assert error.value.detail["code"] == "ai_consent_required"
    assert error.value.detail["decision"] == "not_recorded"


def test_exact_policy_grant_is_server_timestamped_and_authorizes(monkeypatch):
    repository = consent.InMemoryConsentRepository()
    service = _service(repository)
    monkeypatch.setenv("ELLA_AI_CONSENT_ENFORCEMENT_UIDS", "user-a")

    result = service.submit("user-a", _submission())

    assert result["authorized"] is True
    assert result["receipt_created"] is True
    assert result["receipt"]["receipt_id"].startswith("aicr_")
    assert "user-a" not in result["receipt"]["receipt_id"]
    assert result["receipt"]["subject_uid"] == "user-a"
    assert result["receipt"]["profile_binding_id"] == consent.derive_profile_binding_id(
        account_uid="user-a",
        profile_uid="user-a",
    )
    assert result["receipt"]["scope_version"] == consent.CURRENT_SCOPE_VERSION
    assert result["receipt"]["scope_hash"] == consent.CURRENT_SCOPE_HASH
    assert result["receipt"]["processor_ids"] == [processor["id"] for processor in consent.PROCESSORS]
    assert result["receipt"]["server_decided_at"] == "2026-07-26T23:45:00+00:00"
    assert result["receipt"]["build_number"] == "804"
    assert result["enforcement_required"] is True


def test_same_request_retry_dedupes_and_payload_change_conflicts():
    service = _service()

    first = service.submit("user-a", _submission())
    replay = service.submit("user-a", _submission())

    assert replay["receipt_created"] is False
    assert replay["receipt"]["receipt_id"] == first["receipt"]["receipt_id"]

    with pytest.raises(consent.ConsentIdempotencyConflict):
        service.submit("user-a", _submission(decision="revoked"))


def test_firestore_transaction_writes_immutable_receipt_and_current_pointer():
    class Snapshot:
        def __init__(self, exists, data=None):
            self.exists = exists
            self._data = data or {}

        def to_dict(self):
            return dict(self._data)

    class Ref:
        def __init__(self, snapshot):
            self.snapshot = snapshot

        def get(self, transaction):
            assert transaction is transaction_instance
            return self.snapshot

    class Transaction:
        def __init__(self):
            self.writes = []

        def set(self, ref, data, merge=False):
            self.writes.append((ref, data, merge))

    transaction_instance = Transaction()
    user_ref = Ref(Snapshot(True, {"existing": "preserved"}))
    receipt_ref = Ref(Snapshot(False))
    receipt = {
        "receipt_id": "aicr_receipt",
        "decision": "granted",
        "policy_version": consent.CURRENT_POLICY_VERSION,
        "processor_set_hash": consent.CURRENT_PROCESSOR_SET_HASH,
        "server_decided_at": "2026-07-26T23:45:00+00:00",
        "app_version": "1.0.0",
        "build_number": "804",
        "locale": "en-US",
    }

    stored, state, created = consent._record_firestore_receipt.to_wrap(
        transaction_instance,
        user_ref,
        receipt_ref,
        receipt,
        "fingerprint",
    )

    assert created is True
    assert stored["request_fingerprint"] == "fingerprint"
    assert state["receipt_id"] == "aicr_receipt"
    assert transaction_instance.writes[0][0] is receipt_ref
    assert transaction_instance.writes[1] == (
        user_ref,
        {
            "ai_consent": state,
            "private_cloud_sync_enabled": True,
        },
        True,
    )


def test_firestore_transaction_replay_does_not_rewrite_current_state():
    class Snapshot:
        def __init__(self, data):
            self.exists = True
            self._data = data

        def to_dict(self):
            return dict(self._data)

    class Ref:
        def __init__(self, data):
            self.snapshot = Snapshot(data)

        def get(self, transaction):
            return self.snapshot

    class Transaction:
        def set(self, *_args, **_kwargs):
            raise AssertionError("idempotent replay must not write")

    current_state = {"decision": "revoked", "receipt_id": "aicr_newer"}
    stored, state, created = consent._record_firestore_receipt.to_wrap(
        Transaction(),
        Ref({"ai_consent": current_state}),
        Ref({"request_fingerprint": "same", "receipt_id": "aicr_original"}),
        {"decision": "granted"},
        "same",
    )

    assert created is False
    assert stored["receipt_id"] == "aicr_original"
    assert state == current_state


def test_firestore_current_pointer_and_receipt_are_read_in_one_transaction():
    class Snapshot:
        def __init__(self, data):
            self.exists = data is not None
            self._data = data or {}

        def to_dict(self):
            return dict(self._data)

    class ReceiptRef:
        def __init__(self, snapshot):
            self.snapshot = snapshot

        def get(self, transaction):
            assert transaction is transaction_instance
            return self.snapshot

    class ReceiptCollection:
        def document(self, receipt_id):
            assert receipt_id == "aicr_revoked"
            return ReceiptRef(
                Snapshot(
                    {
                        "receipt_id": "aicr_revoked",
                        "decision": "revoked",
                    }
                )
            )

    class UserRef:
        def get(self, transaction):
            assert transaction is transaction_instance
            return Snapshot(
                {
                    "ai_consent": {
                        "receipt_id": "aicr_revoked",
                        "decision": "revoked",
                    }
                }
            )

        def collection(self, name):
            assert name == "ai_consent_receipts"
            return ReceiptCollection()

    transaction_instance = object()
    state, receipt = consent._read_firestore_current_receipt.to_wrap(
        transaction_instance,
        UserRef(),
    )

    assert state["receipt_id"] == "aicr_revoked"
    assert receipt["receipt_id"] == "aicr_revoked"
    assert receipt["decision"] == "revoked"


def test_stale_grant_is_rejected_but_stale_decline_is_recorded():
    service = _service()

    with pytest.raises(consent.ConsentPolicyMismatch):
        service.submit(
            "user-a",
            _submission(
                policy_version="ai-data-processors-v3",
                processor_set_hash="sha256:stale",
            ),
        )

    declined = service.submit(
        "user-a",
        _submission(
            decision="declined",
            request_id="request-decline",
            policy_version="ai-data-processors-v3",
            processor_set_hash="sha256:stale",
        ),
    )
    assert declined["authorized"] is False
    assert declined["consent"]["decision"] == "declined"


def test_v6_grant_is_rejected_and_cannot_pass_protected_route_gate(
    monkeypatch,
):
    repository = consent.InMemoryConsentRepository()
    service = _service(repository)
    with pytest.raises(consent.ConsentPolicyMismatch):
        service.submit(
            "user-a",
            _submission(policy_version="ai-data-processors-v6"),
        )

    current = service.submit("user-a", _submission())
    receipt_id = current["receipt"]["receipt_id"]
    repository.states["user-a"]["policy_version"] = "ai-data-processors-v6"
    repository.receipts[("user-a", receipt_id)]["policy_version"] = "ai-data-processors-v6"
    monkeypatch.setattr(consent, "_repository", repository)
    monkeypatch.setenv("ELLA_AI_CONSENT_ENFORCEMENT_UIDS", "user-a")

    with pytest.raises(HTTPException) as error:
        consent.assert_current_ai_consent("user-a")

    assert error.value.status_code == 403
    assert error.value.detail["required_policy_version"] == ("ai-data-processors-v10")


def test_revoke_supersedes_prior_grant():
    service = _service()
    service.submit("user-a", _submission())

    revoked = service.submit(
        "user-a",
        _submission(decision="revoked", request_id="request-revoke"),
    )

    assert revoked["authorized"] is False
    assert revoked["consent"]["decision"] == "revoked"
    assert revoked["account_deletion"]["path"] == "/v1/users/delete-account"


def _enable_managed_cloud(monkeypatch, uid="user-a"):
    monkeypatch.setenv("ELLA_MANAGED_CLOUD_REAL_DATA_ENABLED_UIDS", uid)


def _assert_exact_managed_cloud_consent(uid="user-a", profile_uid="user-a"):
    return consent.assert_managed_cloud_consent(
        uid,
        profile_uid=profile_uid,
        runtime_provider=consent.MANAGED_CLOUD_RUNTIME_PROVIDER,
        model_route=consent.MANAGED_CLOUD_MODEL_ROUTE,
        memory_provider=consent.MANAGED_CLOUD_MEMORY_PROVIDER,
        photon_scope=consent.MANAGED_CLOUD_PHOTON_SCOPE,
    )


def test_managed_cloud_real_data_defaults_off_even_with_exact_v7_grant(monkeypatch):
    repository = consent.InMemoryConsentRepository()
    _service(repository).submit("user-a", _submission())
    monkeypatch.setattr(consent, "_repository", repository)
    monkeypatch.delenv("ELLA_MANAGED_CLOUD_REAL_DATA_ENABLED", raising=False)
    monkeypatch.delenv("ELLA_MANAGED_CLOUD_REAL_DATA_ENABLED_UIDS", raising=False)

    with pytest.raises(consent.ManagedCloudConsentError) as error:
        _assert_exact_managed_cloud_consent()

    assert error.value.code == "managed_cloud_real_data_disabled"


def test_exact_v7_account_profile_and_scope_authorize_managed_cloud(monkeypatch):
    repository = consent.InMemoryConsentRepository()
    result = _service(repository).submit("user-a", _submission())
    monkeypatch.setattr(consent, "_repository", repository)
    _enable_managed_cloud(monkeypatch)

    assert _assert_exact_managed_cloud_consent() == result["consent"]["receipt_id"]
    assert result["consent"]["profile_binding_id"] == consent.derive_profile_binding_id(
        account_uid="user-a",
        profile_uid="user-a",
    )


def test_revocation_cannot_interleave_between_current_pointer_and_receipt_reads(
    monkeypatch,
):
    class RevocationAtReadRepository(consent.InMemoryConsentRepository):
        def __init__(self):
            super().__init__()
            self.service = None
            self.revoked = False
            self.legacy_state_reads = 0

        def get_state(self, uid):
            self.legacy_state_reads += 1
            return super().get_state(uid)

        def get_current(self, uid):
            if not self.revoked:
                self.revoked = True
                self.service.submit(
                    uid,
                    _submission(
                        decision="revoked",
                        request_id="request-race-revoke",
                    ),
                )
            return super().get_current(uid)

    repository = RevocationAtReadRepository()
    service = _service(repository)
    repository.service = service
    service.submit("user-a", _submission())
    monkeypatch.setattr(consent, "_repository", repository)
    _enable_managed_cloud(monkeypatch)

    with pytest.raises(consent.ManagedCloudConsentError) as error:
        _assert_exact_managed_cloud_consent()

    assert error.value.code == "managed_cloud_consent_required"
    assert repository.legacy_state_reads == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("runtime_provider", "hermes"),
        ("model_route", "openai/gpt-5.6-terra"),
        ("memory_provider", "honcho-self-hosted"),
        (
            "photon_scope",
            "shared_test_line_explicit_contact_v1;allow_all=true;caregiver=false;attachments=false",
        ),
    ],
)
def test_managed_cloud_route_provider_or_scope_drift_fails_closed(monkeypatch, field, value):
    repository = consent.InMemoryConsentRepository()
    _service(repository).submit("user-a", _submission())
    monkeypatch.setattr(consent, "_repository", repository)
    _enable_managed_cloud(monkeypatch)
    route = {
        "profile_uid": "user-a",
        "runtime_provider": consent.MANAGED_CLOUD_RUNTIME_PROVIDER,
        "model_route": consent.MANAGED_CLOUD_MODEL_ROUTE,
        "memory_provider": consent.MANAGED_CLOUD_MEMORY_PROVIDER,
        "photon_scope": consent.MANAGED_CLOUD_PHOTON_SCOPE,
    }
    route[field] = value

    with pytest.raises(consent.ManagedCloudConsentError) as error:
        consent.assert_managed_cloud_consent("user-a", **route)

    assert error.value.code == "managed_cloud_consent_scope_drift"


def test_managed_cloud_account_and_profile_switches_fail_closed(monkeypatch):
    repository = consent.InMemoryConsentRepository()
    _service(repository).submit("user-a", _submission())
    monkeypatch.setattr(consent, "_repository", repository)
    _enable_managed_cloud(monkeypatch, "user-a,user-b")

    with pytest.raises(consent.ManagedCloudConsentError) as account_error:
        _assert_exact_managed_cloud_consent(uid="user-b", profile_uid="user-a")
    assert account_error.value.code == "managed_cloud_consent_required"

    with pytest.raises(consent.ManagedCloudConsentError) as profile_error:
        _assert_exact_managed_cloud_consent(uid="user-a", profile_uid="profile-b")
    assert profile_error.value.code == "managed_cloud_consent_stale"


@pytest.mark.parametrize("terminal_state", ["declined", "revoked", "deleted"])
def test_managed_cloud_decline_revoke_and_delete_fail_closed(monkeypatch, terminal_state):
    repository = consent.InMemoryConsentRepository()
    service = _service(repository)
    service.submit("user-a", _submission())
    if terminal_state == "deleted":
        repository.states.pop("user-a")
        repository.receipts = {key: value for key, value in repository.receipts.items() if key[0] != "user-a"}
    else:
        service.submit(
            "user-a",
            _submission(
                decision=terminal_state,
                request_id=f"request-{terminal_state}",
            ),
        )
    monkeypatch.setattr(consent, "_repository", repository)
    _enable_managed_cloud(monkeypatch)

    with pytest.raises(consent.ManagedCloudConsentError) as error:
        _assert_exact_managed_cloud_consent()

    assert error.value.code == "managed_cloud_consent_required"


def test_missing_or_mutated_immutable_receipt_fails_closed(monkeypatch):
    repository = consent.InMemoryConsentRepository()
    result = _service(repository).submit("user-a", _submission())
    monkeypatch.setattr(consent, "_repository", repository)
    _enable_managed_cloud(monkeypatch)
    receipt_id = result["receipt"]["receipt_id"]

    repository.receipts.pop(("user-a", receipt_id))
    with pytest.raises(consent.ManagedCloudConsentError) as missing:
        _assert_exact_managed_cloud_consent()
    assert missing.value.code == "managed_cloud_consent_required"

    repository.receipts[("user-a", receipt_id)] = {
        **result["receipt"],
        "processor_set_hash": "sha256:mutated",
    }
    with pytest.raises(consent.ManagedCloudConsentError) as mutated:
        _assert_exact_managed_cloud_consent()
    assert mutated.value.code == "managed_cloud_consent_required"


def test_v6_or_malformed_server_receipt_cannot_authorize_managed_cloud(monkeypatch):
    repository = consent.InMemoryConsentRepository()
    _service(repository).submit("user-a", _submission())
    monkeypatch.setattr(consent, "_repository", repository)
    _enable_managed_cloud(monkeypatch)

    repository.states["user-a"]["policy_version"] = "ai-data-processors-v6"
    with pytest.raises(consent.ManagedCloudConsentError):
        _assert_exact_managed_cloud_consent()

    repository.states["user-a"]["policy_version"] = consent.CURRENT_POLICY_VERSION
    repository.states["user-a"]["server_decided_at"] = "not-a-timestamp"
    with pytest.raises(consent.ManagedCloudConsentError):
        _assert_exact_managed_cloud_consent()


@pytest.mark.parametrize("decision", ["declined", "revoked"])
def test_decline_and_revoke_block_central_target_uid_egress(monkeypatch, decision):
    repository = consent.InMemoryConsentRepository()
    service = _service(repository)
    service.submit("user-a", _submission())
    service.submit(
        "user-a",
        _submission(decision=decision, request_id=f"request-{decision}"),
    )
    monkeypatch.setattr(consent, "_repository", repository)
    monkeypatch.setenv("ELLA_AI_CONSENT_ENFORCEMENT_UIDS", "user-a")

    with pytest.raises(HTTPException) as error:
        consent.assert_current_ai_consent("user-a")

    assert error.value.status_code == 403
    assert error.value.detail["code"] == "ai_consent_required"
    assert error.value.detail["decision"] == decision


def test_account_deletion_receipt_is_opaque_and_completed():
    receipt = consent.build_account_deletion_receipt(now=lambda: datetime(2026, 7, 26, 20, 15, tzinfo=timezone.utc))

    assert receipt["request_id"].startswith("aidel_")
    assert len(receipt["request_id"]) == len("aidel_") + 32
    assert receipt["status"] == "completed"
    assert receipt["scope"] == "account_and_user_data"
    assert receipt["server_completed_at"] == "2026-07-26T20:15:00+00:00"
    assert "uid" not in receipt


def test_receipts_are_user_scoped():
    repository = consent.InMemoryConsentRepository()
    service = _service(repository)
    result = service.submit("user-a", _submission())
    receipt_id = result["receipt"]["receipt_id"]

    assert service.receipt("user-a", receipt_id)["subject_uid"] == "user-a"
    assert service.receipt("user-b", receipt_id) is None


def test_enforcement_defaults_off_and_supports_uid_canary(monkeypatch):
    monkeypatch.delenv("ELLA_AI_CONSENT_ENFORCEMENT_ENABLED", raising=False)
    monkeypatch.delenv("ELLA_AI_CONSENT_ENFORCEMENT_UIDS", raising=False)
    assert consent.ai_consent_enforcement_required("user-a") is False

    monkeypatch.setenv("ELLA_AI_CONSENT_ENFORCEMENT_UIDS", "user-a,user-b")
    assert consent.ai_consent_enforcement_required("user-a") is True
    assert consent.ai_consent_enforcement_required("user-c") is False


def test_tts_gate_accepts_configured_internal_service_token(monkeypatch):
    repository = consent.InMemoryConsentRepository()
    service = _service(repository)
    service.submit("user-a", _submission())
    monkeypatch.setattr(consent, "_repository", repository)
    monkeypatch.setenv("ELLA_AI_CONSENT_ENFORCEMENT_ENABLED", "true")
    monkeypatch.setenv("ELLA_INTERNAL_VOICE_TTS_TOKEN", "internal-secret")

    assert (
        consent.require_current_ai_consent_or_internal_tts(
            authorization=None,
            x_internal_token="internal-secret",
            x_subject_uid="user-a",
        )
        == "user-a"
    )


def test_tts_internal_service_token_cannot_bypass_subject_consent(monkeypatch):
    monkeypatch.setenv("ELLA_AI_CONSENT_ENFORCEMENT_ENABLED", "true")
    monkeypatch.setenv("ELLA_INTERNAL_VOICE_TTS_TOKEN", "internal-secret")

    with pytest.raises(HTTPException) as missing_subject:
        consent.require_current_ai_consent_or_internal_tts(
            authorization=None,
            x_internal_token="internal-secret",
            x_subject_uid=None,
        )
    assert missing_subject.value.detail == {"code": "ai_consent_subject_required"}

    repository = consent.InMemoryConsentRepository()
    monkeypatch.setattr(consent, "_repository", repository)
    with pytest.raises(HTTPException) as missing_consent:
        consent.require_current_ai_consent_or_internal_tts(
            authorization=None,
            x_internal_token="internal-secret",
            x_subject_uid="user-a",
        )
    assert missing_consent.value.detail["code"] == "ai_consent_required"


def test_tts_uid_canary_rejects_unattributed_legacy_callers(monkeypatch):
    monkeypatch.setenv("ELLA_AI_CONSENT_ENFORCEMENT_UIDS", "user-a")
    monkeypatch.delenv("ELLA_AI_CONSENT_ENFORCEMENT_ENABLED", raising=False)
    monkeypatch.delenv("ELLA_INTERNAL_VOICE_TTS_TOKEN", raising=False)

    with pytest.raises(HTTPException) as error:
        consent.require_current_ai_consent_or_internal_tts(
            authorization=None,
            x_internal_token=None,
            x_subject_uid=None,
        )

    assert error.value.status_code == 401
    assert error.value.detail == {"code": "authorization_required"}


def test_tts_global_enforcement_rejects_unattributed_legacy_callers(monkeypatch):
    monkeypatch.setenv("ELLA_AI_CONSENT_ENFORCEMENT_ENABLED", "true")
    monkeypatch.delenv("ELLA_AI_CONSENT_ENFORCEMENT_UIDS", raising=False)
    monkeypatch.delenv("ELLA_INTERNAL_VOICE_TTS_TOKEN", raising=False)

    with pytest.raises(HTTPException) as error:
        consent.require_current_ai_consent_or_internal_tts(
            authorization=None,
            x_internal_token=None,
            x_subject_uid=None,
        )

    assert error.value.status_code == 401
    assert error.value.detail == {"code": "authorization_required"}


def test_tts_authenticated_canary_subject_must_have_current_consent(monkeypatch):
    repository = consent.InMemoryConsentRepository()
    monkeypatch.setattr(consent, "_repository", repository)
    monkeypatch.setattr(consent, "get_exact_firebase_uid", lambda *_args: "user-a")
    monkeypatch.setenv("ELLA_AI_CONSENT_ENFORCEMENT_UIDS", "user-a")

    with pytest.raises(HTTPException) as error:
        consent.require_current_ai_consent_or_internal_tts(
            authorization="Bearer firebase-token",
            x_internal_token=None,
            x_subject_uid=None,
        )

    assert error.value.status_code == 403
    assert error.value.detail["code"] == "ai_consent_required"


def test_provider_aliases_resolve_to_disclosed_legal_recipient():
    assert consent.resolve_processor("soniox")["legal_recipient"] == "Soniox"
    assert consent.resolve_processor("speechmatics")["legal_recipient"] == "Speechmatics"
    assert consent.resolve_processor("kokoro")["third_party"] is False
    assert consent.resolve_processor("inworld")["legal_recipient"] == "Inworld AI"
    assert consent.resolve_processor("gemini-native-live")["legal_recipient"] == "Google Gemini"
    assert consent.resolve_processor("grok-voice")["legal_recipient"] == "xAI Grok"
    assert consent.resolve_processor("unknown") is None


def test_router_returns_conflict_for_stale_grant(monkeypatch):
    service = _service()
    monkeypatch.setattr(ai_consent, "get_ai_consent_service", lambda: service)
    request = ai_consent.AiConsentSubmissionRequest(
        decision="granted",
        policy_version="ai-data-processors-v3",
        processor_set_hash="sha256:stale",
        request_id="request-stale",
        app_version="1.0.0",
        build_number="804",
        locale="en-US",
    )

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            ai_consent.submit_ai_consent(
                request,
                identity=FirebaseTokenIdentity(
                    uid="user-a",
                    verified_email="user-a@example.invalid",
                ),
            )
        )

    assert error.value.status_code == 409
    assert error.value.detail["code"] == "ai_consent_policy_mismatch"


def test_managed_cloud_consent_orders_denial_before_firestore_and_grant_after(
    monkeypatch,
):
    uid = "user-a"
    repository = consent.InMemoryConsentRepository()
    service = _service(repository)
    events = []
    original_record = repository.record

    def record(*args, **kwargs):
        events.append(f"firestore:{args[2]['decision']}")
        return original_record(*args, **kwargs)

    async def deny(**kwargs):
        events.append(f"postgres:{kwargs['decision']}")
        return {"decision": kwargs["decision"]}

    async def grant(**kwargs):
        events.append("postgres:granted")
        return {"decision": "granted"}

    monkeypatch.setenv("ELLA_MANAGED_CLOUD_REAL_DATA_ENABLED_UIDS", uid)
    monkeypatch.setattr(repository, "record", record)
    monkeypatch.setattr(
        consent_authority.managed_cloud_consent,
        "synchronize_denial",
        deny,
    )
    monkeypatch.setattr(
        consent_authority.managed_cloud_consent,
        "synchronize_grant",
        grant,
    )

    asyncio.run(
        consent_authority.submit_with_managed_cloud_authority(
            uid=uid,
            submission=_submission(request_id="request-grant-order"),
            service=service,
        )
    )
    asyncio.run(
        consent_authority.submit_with_managed_cloud_authority(
            uid=uid,
            submission=_submission(
                decision="revoked",
                request_id="request-revoke-order",
            ),
            service=service,
        )
    )

    assert events == [
        "firestore:granted",
        "postgres:granted",
        "postgres:revoked",
        "firestore:revoked",
    ]


def test_self_hosted_grant_passes_only_verified_email_to_authority(monkeypatch):
    uid = "user-a"
    service = _service()
    captured = {}

    async def grant(**kwargs):
        captured.update(kwargs)
        return {"decision": "granted"}

    monkeypatch.setenv("ELLA_SELF_HOSTED_PROVISIONING_ENABLED", "true")
    monkeypatch.setenv("ELLA_SELF_HOSTED_PROVISIONING_RELAX_FRESH_UID", "true")
    monkeypatch.setattr(
        consent_authority.managed_cloud_consent,
        "synchronize_grant",
        grant,
    )

    asyncio.run(
        consent_authority.submit_with_managed_cloud_authority(
            uid=uid,
            verified_email="User-A@Example.invalid",
            submission=_submission(request_id="request-verified-email-propagation"),
            service=service,
        )
    )

    assert captured["allow_fresh_uid_bootstrap"] is True
    assert captured["bootstrap_email"] == "User-A@Example.invalid"


def test_managed_cloud_denial_authority_error_prevents_firestore_mutation(
    monkeypatch,
):
    uid = "user-a"
    repository = consent.InMemoryConsentRepository()
    service = _service(repository)
    firestore_calls = 0
    original_record = repository.record

    def record(*args, **kwargs):
        nonlocal firestore_calls
        firestore_calls += 1
        return original_record(*args, **kwargs)

    async def unavailable(**_kwargs):
        raise managed_cloud_consent.ManagedCloudAuthorityUnavailable("managed_cloud_authority_unavailable")

    monkeypatch.setenv("ELLA_MANAGED_CLOUD_REAL_DATA_ENABLED_UIDS", uid)
    monkeypatch.setattr(repository, "record", record)
    monkeypatch.setattr(
        consent_authority.managed_cloud_consent,
        "synchronize_denial",
        unavailable,
    )

    with pytest.raises(managed_cloud_consent.ManagedCloudAuthorityUnavailable):
        asyncio.run(
            consent_authority.submit_with_managed_cloud_authority(
                uid=uid,
                submission=_submission(
                    decision="revoked",
                    request_id="request-revoke-unavailable",
                ),
                service=service,
            )
        )

    assert firestore_calls == 0


def test_managed_cloud_grant_authority_error_returns_failure_after_receipt(
    monkeypatch,
):
    uid = "user-a"
    repository = consent.InMemoryConsentRepository()
    service = _service(repository)

    async def unavailable(**_kwargs):
        raise managed_cloud_consent.ManagedCloudAuthorityUnavailable("managed_cloud_authority_unavailable")

    monkeypatch.setenv("ELLA_MANAGED_CLOUD_REAL_DATA_ENABLED_UIDS", uid)
    monkeypatch.setattr(
        consent_authority.managed_cloud_consent,
        "synchronize_grant",
        unavailable,
    )

    with pytest.raises(managed_cloud_consent.ManagedCloudAuthorityUnavailable):
        asyncio.run(
            consent_authority.submit_with_managed_cloud_authority(
                uid=uid,
                submission=_submission(request_id="request-grant-unavailable"),
                service=service,
            )
        )

    # The immutable receipt may exist, but no successful authority publication
    # was acknowledged. The caller receives 503 and redemption still requires
    # an exact PostgreSQL authority row behind its transaction lock.
    assert service.status(uid)["authorized"] is True


def test_submission_rejects_device_identifiers_and_unknown_metadata():
    with pytest.raises(ValidationError):
        ai_consent.AiConsentSubmissionRequest(
            decision="granted",
            policy_version=consent.CURRENT_POLICY_VERSION,
            processor_set_hash=consent.CURRENT_PROCESSOR_SET_HASH,
            request_id="request-device",
            app_version="1.0.0",
            build_number="804",
            locale="en-US",
            device_id="do-not-store-this",
        )


def test_policy_is_public_but_status_and_receipts_require_firebase_auth(monkeypatch):
    service = _service()
    monkeypatch.setattr(ai_consent, "get_ai_consent_service", lambda: service)
    app = FastAPI()
    app.include_router(ai_consent.router)
    client = TestClient(app)

    policy_response = client.get("/v1/users/ai-consent/policy")
    assert policy_response.status_code == 200
    assert policy_response.json()["version"] == consent.CURRENT_POLICY_VERSION

    assert client.get("/v1/users/ai-consent").status_code == 401
    assert client.get("/v1/users/ai-consent/receipts/aicr_unknown").status_code == 401

    app.dependency_overrides[get_exact_firebase_uid] = lambda: "user-a"
    status_response = client.get("/v1/users/ai-consent")
    assert status_response.status_code == 200
    assert status_response.json()["subject_uid"] == "user-a"


def test_authenticated_api_records_exact_v7_profile_bound_receipt(monkeypatch):
    service = _service()
    monkeypatch.setattr(ai_consent, "get_ai_consent_service", lambda: service)
    app = FastAPI()
    app.include_router(ai_consent.router)
    app.dependency_overrides[get_firebase_token_identity] = lambda: FirebaseTokenIdentity(
        uid="user-a",
        verified_email="user-a@example.invalid",
    )
    client = TestClient(app)

    response = client.post(
        "/v1/users/ai-consent",
        json={
            "decision": "granted",
            "policy_version": consent.CURRENT_POLICY_VERSION,
            "processor_set_hash": consent.CURRENT_PROCESSOR_SET_HASH,
            "scope_version": consent.CURRENT_SCOPE_VERSION,
            "scope_hash": consent.CURRENT_SCOPE_HASH,
            "request_id": "request-api-v7",
            "app_version": "1.0.0",
            "build_number": "804",
            "locale": "en-US",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["authorized"] is True
    assert body["receipt"]["subject_uid"] == "user-a"
    assert body["receipt"]["profile_binding_id"] == consent.derive_profile_binding_id(
        account_uid="user-a",
        profile_uid="user-a",
    )
    assert body["receipt"]["policy_version"] == consent.CURRENT_POLICY_VERSION
    assert body["receipt"]["processor_set_hash"] == consent.CURRENT_PROCESSOR_SET_HASH
    assert body["receipt"]["scope_version"] == consent.CURRENT_SCOPE_VERSION
    assert body["receipt"]["scope_hash"] == consent.CURRENT_SCOPE_HASH
    assert body["receipt"]["server_decided_at"] == "2026-07-26T23:45:00+00:00"


@pytest.mark.parametrize("decision", ("declined", "revoked"))
def test_authenticated_api_allows_terminal_decisions_without_verified_email(monkeypatch, decision):
    service = _service()
    denials = []

    async def deny(**kwargs):
        denials.append(kwargs)
        return {"decision": kwargs["decision"], "authority_absent": True}

    monkeypatch.setattr(ai_consent, "get_ai_consent_service", lambda: service)
    monkeypatch.delenv("ELLA_HERMES_CLOUD_PROVISIONING_ENABLED", raising=False)
    monkeypatch.delenv("ELLA_HERMES_CLOUD_PROVISIONING_ENABLED_UIDS", raising=False)
    monkeypatch.setenv("ELLA_SELF_HOSTED_PROVISIONING_ENABLED", "true")
    monkeypatch.setattr(
        consent_authority.managed_cloud_consent,
        "synchronize_denial",
        deny,
    )
    app = FastAPI()
    app.include_router(ai_consent.router)
    app.dependency_overrides[get_firebase_token_identity] = lambda: FirebaseTokenIdentity(uid="user-a")
    client = TestClient(app)

    response = client.post(
        "/v1/users/ai-consent",
        json={
            "decision": decision,
            "policy_version": consent.CURRENT_POLICY_VERSION,
            "processor_set_hash": consent.CURRENT_PROCESSOR_SET_HASH,
            "scope_version": consent.CURRENT_SCOPE_VERSION,
            "scope_hash": consent.CURRENT_SCOPE_HASH,
            "request_id": f"request-api-{decision}",
            "app_version": "1.0.0",
            "build_number": "804",
            "locale": "en-US",
        },
    )

    assert response.status_code == 200
    assert response.json()["receipt"]["decision"] == decision
    assert denials == [
        {
            "uid": "user-a",
            "decision": decision,
            "verified_email": "",
        }
    ]
