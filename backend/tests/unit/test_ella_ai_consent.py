import asyncio
import hashlib
import threading
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
    account_epoch_token="",
    account_epoch_auth_time=0,
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
        account_epoch_token=account_epoch_token,
        account_epoch_auth_time=account_epoch_auth_time,
    )


def _service(repository=None):
    return consent.AiConsentService(
        repository or consent.InMemoryConsentRepository(),
        now=lambda: datetime(2026, 7, 26, 23, 45, tzinfo=timezone.utc),
    )


def test_policy_matches_exact_managed_cloud_v10_artwork_contract():
    policy = consent.AiConsentService.policy()

    assert policy["version"] == "ai-data-processors-v10"
    assert policy["minimum_required_version"] == "ai-data-processors-v10"
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


def test_firestore_account_deletion_completion_updates_receipt_and_state_atomically():
    class Snapshot:
        def __init__(self, data):
            self.exists = data is not None
            self._data = data or {}

        def to_dict(self):
            return dict(self._data)

    class Ref:
        def __init__(self, data):
            self.snapshot = Snapshot(data)

        def get(self, transaction):
            assert transaction is transaction_instance
            return self.snapshot

    class Transaction:
        def __init__(self):
            self.writes = []

        def set(self, ref, data, merge=False):
            self.writes.append((ref, data, merge))

    receipt = {
        "receipt_id": "aicr_delete",
        "decision": "deleted",
        "request_id": "request-delete",
        "deletion_phase": "pending",
    }
    state = {
        "receipt_id": "aicr_delete",
        "decision": "deleted",
        "deletion_phase": "pending",
    }
    transaction_instance = Transaction()
    user_ref = Ref({"ai_consent": state})
    receipt_ref = Ref(receipt)

    completed_receipt, completed_state, created = consent._complete_firestore_account_deletion.to_wrap(
        transaction_instance,
        user_ref,
        receipt_ref,
        "request-delete",
        "2026-09-26T04:30:00+00:00",
        "post-deletion-account-epoch-token",
    )

    assert created is True
    assert completed_receipt["deletion_phase"] == "completed"
    assert completed_state["deletion_completed_at"] == "2026-09-26T04:30:00+00:00"
    assert "account_epoch_token" not in completed_state
    assert completed_receipt["account_epoch_token"] == "post-deletion-account-epoch-token"
    assert completed_state["account_epoch_hash"] == completed_receipt["account_epoch_hash"]
    assert transaction_instance.writes == [
        (receipt_ref, completed_receipt, False),
        (user_ref, {"ai_consent": completed_state}, True),
    ]


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


def test_nonmaterial_policy_metadata_drift_keeps_explicit_grant_current(monkeypatch):
    repository = consent.InMemoryConsentRepository()
    service = _service(repository)
    result = service.submit("user-a", _submission())
    receipt_id = result["receipt"]["receipt_id"]

    archived_metadata = {
        "processor_set_hash": "sha256:accepted-descriptor",
        "processor_ids": ["accepted-processor"],
        "scope_version": "accepted-scope-descriptor",
        "scope_hash": "sha256:accepted-scope-descriptor",
    }
    repository.states["user-a"].update(archived_metadata)
    repository.receipts[("user-a", receipt_id)].update(archived_metadata)
    monkeypatch.setattr(consent, "CURRENT_POLICY_VERSION", "ai-data-processors-v11")
    monkeypatch.setattr(consent, "CURRENT_PROCESSOR_SET_HASH", "sha256:deployed-descriptor")
    monkeypatch.setattr(consent, "CURRENT_SCOPE_VERSION", "deployed-scope-descriptor")
    monkeypatch.setattr(consent, "CURRENT_SCOPE_HASH", "sha256:deployed-scope-descriptor")
    monkeypatch.setattr(consent, "_repository", repository)
    _enable_managed_cloud(monkeypatch)

    status = service.status("user-a")

    assert status["authorized"] is True
    assert status["authority_state"] == "authorized"
    assert status["retryable"] is False
    assert _assert_exact_managed_cloud_consent() == receipt_id


def test_future_explicit_grant_remains_current_after_server_rollback():
    repository = consent.InMemoryConsentRepository()
    service = _service(repository)
    result = service.submit("user-a", _submission())
    receipt_id = result["receipt"]["receipt_id"]
    repository.states["user-a"]["policy_version"] = "ai-data-processors-v11"
    repository.receipts[("user-a", receipt_id)]["policy_version"] = "ai-data-processors-v11"

    status = service.status("user-a")

    assert status["authorized"] is True
    assert status["authority_state"] == "authorized"


def test_human_bumped_minimum_policy_requires_reconsent(monkeypatch):
    repository = consent.InMemoryConsentRepository()
    service = _service(repository)
    service.submit("user-a", _submission())
    monkeypatch.setattr(
        consent,
        "CONSENT_POLICY_VERSION_ORDER",
        (*consent.CONSENT_POLICY_VERSION_ORDER, "ai-data-processors-v11"),
    )
    monkeypatch.setattr(consent, "MINIMUM_REQUIRED_POLICY_VERSION", "ai-data-processors-v11")
    monkeypatch.setattr(consent, "_repository", repository)
    monkeypatch.setenv("ELLA_AI_CONSENT_ENFORCEMENT_UIDS", "user-a")

    status = service.status("user-a")
    assert status["authorized"] is False
    assert status["authority_state"] == "reconsent_required"
    assert status["retryable"] is False

    with pytest.raises(HTTPException) as error:
        consent.assert_current_ai_consent("user-a")
    assert error.value.status_code == 403
    assert error.value.detail == {
        "code": "ai_consent_reconsent_required",
        "authority_state": "reconsent_required",
        "retryable": False,
        "decision": "granted",
        "required_policy_version": "ai-data-processors-v11",
        "required_processor_set_hash": consent.CURRENT_PROCESSOR_SET_HASH,
    }


def test_revoke_supersedes_prior_grant():
    service = _service()
    service.submit("user-a", _submission())

    revoked = service.submit(
        "user-a",
        _submission(decision="revoked", request_id="request-revoke"),
    )

    assert revoked["authorized"] is False
    assert revoked["authority_state"] == "revoked"
    assert revoked["retryable"] is False
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
    assert missing.value.code == "managed_cloud_consent_authority_unavailable"
    assert missing.value.retryable is True

    repository.receipts[("user-a", receipt_id)] = {
        **result["receipt"],
        "processor_set_hash": "sha256:mutated",
    }
    with pytest.raises(consent.ManagedCloudConsentError) as mutated:
        _assert_exact_managed_cloud_consent()
    assert mutated.value.code == "managed_cloud_consent_authority_unavailable"
    assert mutated.value.retryable is True


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


def test_account_deletion_records_server_only_terminal_consent_state():
    service = _service()
    service.submit("user-a", _submission())

    result = service.record_account_deletion("user-a", request_id="aidel_account_delete_0001")

    assert result["authorized"] is False
    assert result["authority_state"] == "deleted"
    assert result["retryable"] is False
    assert result["consent"]["decision"] == "deleted"
    assert result["consent"]["deletion_phase"] == "pending"
    assert result["receipt"]["decision"] == "deleted"


def test_completed_account_deletion_allows_only_a_fresh_explicit_grant():
    service = _service()
    original = service.submit("user-a", _submission(request_id="request-before-delete"))
    service.record_account_deletion("user-a", request_id="request-delete-account")

    with pytest.raises(consent.ConsentAccountDeleted):
        service.submit("user-a", _submission(request_id="request-during-delete"))

    completed = service.complete_account_deletion("user-a", request_id="request-delete-account")
    assert completed["deletion_completed"] is True
    assert completed["consent"]["deletion_phase"] == "completed"

    with pytest.raises(consent.ConsentAccountDeleted):
        service.submit("user-a", _submission(request_id="request-before-delete"))

    assert "account_epoch_token" not in service.status("user-a")
    deleted_status = service.status("user-a", account_epoch_auth_time=1785110000)
    assert "account_epoch_token" not in deleted_status["consent"]
    assert "account_epoch_hash" not in deleted_status["consent"]
    assert deleted_status["account_epoch_token"]

    fresh = service.submit(
        "user-a",
        _submission(
            request_id="request-after-delete",
            account_epoch_token=deleted_status["account_epoch_token"],
            account_epoch_auth_time=1785110000,
        ),
    )
    assert fresh["authorized"] is True
    assert fresh["receipt"]["receipt_id"] != original["receipt"]["receipt_id"]
    assert fresh["consent"].get("deletion_phase") is None
    with pytest.raises(consent.ConsentAccountDeleted):
        service.submit(
            "user-a",
            _submission(
                request_id="request-reusing-consumed-epoch",
                account_epoch_token=deleted_status["account_epoch_token"],
                account_epoch_auth_time=1785110000,
            ),
        )


def test_account_deletion_completion_is_exact_and_idempotent():
    service = _service()
    service.record_account_deletion("user-a", request_id="request-delete-account")

    first = service.complete_account_deletion("user-a", request_id="request-delete-account")
    second = service.complete_account_deletion("user-a", request_id="request-delete-account")

    assert first["deletion_completed"] is True
    assert second["deletion_completed"] is False
    assert second["deletion_completed_at"] == first["deletion_completed_at"]
    assert "account_epoch_token" not in service.status("user-a")
    assert service.status("user-a", account_epoch_auth_time=1785110000)["account_epoch_token"]
    with pytest.raises(consent.ConsentAuthorityUnavailable):
        service.complete_account_deletion("user-a", request_id="request-other-delete")


def test_predeletion_grant_paused_before_firestore_write_cannot_reopen_completed_epoch():
    underlying = consent.InMemoryConsentRepository()
    grant_ready = threading.Event()
    resume_grant = threading.Event()

    class PausingRepository:
        def __getattr__(self, name):
            return getattr(underlying, name)

        def record(self, uid, receipt_id, receipt, request_fingerprint):
            if receipt.get("request_id") == "request-paused-before-delete":
                grant_ready.set()
                assert resume_grant.wait(timeout=5)
            return underlying.record(uid, receipt_id, receipt, request_fingerprint)

    service = _service(PausingRepository())
    result = {}

    def submit_paused_grant():
        try:
            result["payload"] = service.submit(
                "user-a",
                _submission(request_id="request-paused-before-delete"),
            )
        except Exception as exc:
            result["error"] = exc

    grant_thread = threading.Thread(target=submit_paused_grant)
    grant_thread.start()
    assert grant_ready.wait(timeout=5)

    service.record_account_deletion("user-a", request_id="request-delete-account")
    service.complete_account_deletion("user-a", request_id="request-delete-account")
    deleted_status = service.status("user-a", account_epoch_auth_time=1785110000)

    resume_grant.set()
    grant_thread.join(timeout=5)
    assert not grant_thread.is_alive()
    assert isinstance(result.get("error"), consent.ConsentAccountDeleted)
    assert "payload" not in result
    assert service.status("user-a")["authority_state"] == "deleted"

    fresh = service.submit(
        "user-a",
        _submission(
            request_id="request-fresh-after-delete",
            account_epoch_token=deleted_status["account_epoch_token"],
            account_epoch_auth_time=1785110000,
        ),
    )
    assert fresh["authority_state"] == "authorized"
    assert "account_epoch_token" not in fresh


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
        assert await kwargs["grant_is_current"]() is True
        events.append("postgres:granted")
        return {"decision": "granted"}

    async def erase_artwork(uid):
        events.append("artwork:erased")

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
    monkeypatch.setattr(consent_authority, "_erase_artwork_for_denial", erase_artwork)

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
        "artwork:erased",
    ]


def test_pending_account_deletion_tombstone_cannot_be_overwritten_by_new_or_replayed_grant():
    service = _service()
    original = service.submit("user-a", _submission(request_id="request-before-delete"))
    service.record_account_deletion("user-a", request_id="request-delete-account")

    for request_id in ("request-after-delete", "request-before-delete"):
        with pytest.raises(consent.ConsentAccountDeleted):
            service.submit("user-a", _submission(request_id=request_id))

    status = service.status("user-a")
    assert status["authority_state"] == "deleted"
    assert status["consent"]["receipt_id"] != original["receipt"]["receipt_id"]


def test_managed_grant_revalidation_rejects_deletion_that_wins_before_publication(monkeypatch):
    uid = "user-a"
    service = _service()

    async def grant(**kwargs):
        service.record_account_deletion(uid, request_id="request-delete-during-publication")
        assert await kwargs["grant_is_current"]() is False
        raise managed_cloud_consent.ManagedCloudAuthorityUnavailable("managed_cloud_authority_grant_superseded")

    monkeypatch.setenv("ELLA_MANAGED_CLOUD_REAL_DATA_ENABLED_UIDS", uid)
    monkeypatch.setattr(consent_authority.managed_cloud_consent, "synchronize_grant", grant)

    with pytest.raises(managed_cloud_consent.ManagedCloudAuthorityUnavailable):
        asyncio.run(
            consent_authority.submit_with_managed_cloud_authority(
                uid=uid,
                submission=_submission(request_id="request-racing-grant"),
                service=service,
            )
        )

    assert service.status(uid)["authority_state"] == "deleted"


def test_consent_revocation_stays_denied_when_artwork_erasure_is_unavailable(monkeypatch):
    uid = "user-a"
    service = _service()

    async def unavailable(_uid):
        raise consent_authority.ArtworkConsentErasureUnavailable("artwork_consent_erasure_unavailable")

    monkeypatch.setattr(consent_authority, "_erase_artwork_for_denial", unavailable)

    with pytest.raises(consent_authority.ArtworkConsentErasureUnavailable):
        asyncio.run(
            consent_authority.submit_with_managed_cloud_authority(
                uid=uid,
                submission=_submission(decision="revoked", request_id="request-revoke-artwork-unavailable"),
                service=service,
            )
        )

    assert service.status(uid)["authorized"] is False


def test_consent_api_returns_typed_503_after_durable_denial_when_artwork_erasure_is_unavailable(monkeypatch):
    service = _service()

    async def unavailable(_uid):
        raise consent_authority.ArtworkConsentErasureUnavailable("artwork_consent_erasure_unavailable")

    monkeypatch.setattr(ai_consent, "get_ai_consent_service", lambda: service)
    monkeypatch.setattr(consent_authority, "_erase_artwork_for_denial", unavailable)
    monkeypatch.delenv("ELLA_HERMES_CLOUD_PROVISIONING_ENABLED", raising=False)
    monkeypatch.delenv("ELLA_HERMES_CLOUD_PROVISIONING_ENABLED_UIDS", raising=False)
    monkeypatch.delenv("ELLA_SELF_HOSTED_PROVISIONING_ENABLED", raising=False)
    app = FastAPI()
    app.include_router(ai_consent.router)
    app.dependency_overrides[get_firebase_token_identity] = lambda: FirebaseTokenIdentity(uid="user-a")
    client = TestClient(app)

    response = client.post(
        "/v1/users/ai-consent",
        json={
            "decision": "revoked",
            "policy_version": consent.CURRENT_POLICY_VERSION,
            "processor_set_hash": consent.CURRENT_PROCESSOR_SET_HASH,
            "scope_version": consent.CURRENT_SCOPE_VERSION,
            "scope_hash": consent.CURRENT_SCOPE_HASH,
            "request_id": "request-api-revoke-erasure-failure",
            "app_version": "1.0.0",
            "build_number": "804",
            "locale": "en-US",
        },
    )

    assert response.status_code == 503
    assert response.json() == {"detail": {"code": "artwork_consent_erasure_unavailable"}}
    assert service.status("user-a")["authorized"] is False


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

    with pytest.raises(ValidationError):
        ai_consent.AiConsentSubmissionRequest(
            decision="deleted",
            policy_version=consent.CURRENT_POLICY_VERSION,
            processor_set_hash=consent.CURRENT_PROCESSOR_SET_HASH,
            request_id="request-public-delete",
            app_version="1.0.0",
            build_number="804",
            locale="en-US",
            scope_version=consent.CURRENT_SCOPE_VERSION,
            scope_hash=consent.CURRENT_SCOPE_HASH,
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
    app.dependency_overrides[get_firebase_token_identity] = lambda: FirebaseTokenIdentity(uid="user-a")
    status_response = client.get("/v1/users/ai-consent")
    assert status_response.status_code == 200
    assert status_response.json()["subject_uid"] == "user-a"


def test_status_route_reports_repository_failure_as_retryable_unavailable(monkeypatch):
    class UnavailableRepository(consent.InMemoryConsentRepository):
        def get_current(self, uid):
            raise RuntimeError("synthetic datastore outage")

    service = _service(UnavailableRepository())
    monkeypatch.setattr(ai_consent, "get_ai_consent_service", lambda: service)
    app = FastAPI()
    app.include_router(ai_consent.router)
    app.dependency_overrides[get_firebase_token_identity] = lambda: FirebaseTokenIdentity(uid="user-a")

    response = TestClient(app).get("/v1/users/ai-consent")

    assert response.status_code == 503
    assert response.json() == {
        "detail": {
            "code": "ai_consent_authority_unavailable",
            "authority_state": "unavailable",
            "retryable": True,
        }
    }


def test_protected_route_reports_integrity_failure_as_retryable_unavailable(monkeypatch):
    repository = consent.InMemoryConsentRepository()
    result = _service(repository).submit("user-a", _submission())
    repository.receipts.pop(("user-a", result["receipt"]["receipt_id"]))
    monkeypatch.setattr(consent, "_repository", repository)
    monkeypatch.setenv("ELLA_AI_CONSENT_ENFORCEMENT_UIDS", "user-a")

    with pytest.raises(HTTPException) as error:
        consent.assert_current_ai_consent("user-a")

    assert error.value.status_code == 503
    assert error.value.detail == {
        "code": "ai_consent_authority_unavailable",
        "authority_state": "unavailable",
        "retryable": True,
    }


def test_account_deletion_absence_is_terminal_not_retryable():
    status = _service().status("user-a")

    assert status["authorized"] is False
    assert status["authority_state"] == "not_accepted"
    assert status["retryable"] is False


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
    erasures = []

    async def deny(**kwargs):
        denials.append(kwargs)
        return {"decision": kwargs["decision"], "authority_absent": True}

    async def erase_artwork(uid):
        erasures.append(uid)

    monkeypatch.setattr(ai_consent, "get_ai_consent_service", lambda: service)
    monkeypatch.delenv("ELLA_HERMES_CLOUD_PROVISIONING_ENABLED", raising=False)
    monkeypatch.delenv("ELLA_HERMES_CLOUD_PROVISIONING_ENABLED_UIDS", raising=False)
    monkeypatch.setenv("ELLA_SELF_HOSTED_PROVISIONING_ENABLED", "true")
    monkeypatch.setattr(
        consent_authority.managed_cloud_consent,
        "synchronize_denial",
        deny,
    )
    monkeypatch.setattr(consent_authority, "_erase_artwork_for_denial", erase_artwork)
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
    assert erasures == ["user-a"]
