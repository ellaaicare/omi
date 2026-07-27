from datetime import datetime, timezone

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ella.routers import ai_consent
from ella.services import ai_consent as consent


def _submission(
    *,
    decision="granted",
    request_id="request-0001",
    policy_version=consent.CURRENT_POLICY_VERSION,
    processor_set_hash=consent.CURRENT_PROCESSOR_SET_HASH,
):
    return consent.ConsentSubmission(
        decision=decision,
        policy_version=policy_version,
        processor_set_hash=processor_set_hash,
        request_id=request_id,
        app_version="1.0.0",
        build_number="804",
        locale="en-US",
    )


def _service(repository=None):
    return consent.AiConsentService(
        repository or consent.InMemoryConsentRepository(),
        now=lambda: datetime(2026, 7, 26, 23, 45, tzinfo=timezone.utc),
    )


def test_policy_covers_current_chat_voice_stt_and_tts_surface():
    policy = consent.AiConsentService.policy()

    assert policy["version"] == "ai-data-processors-v5"
    assert policy["processor_set_hash"] == "sha256:9c2529babbd6241f20242cf0836baf7e1899d05bb3d945a0d38a357113d4cbc4"
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
    receipt = consent.build_account_deletion_receipt(
        now=lambda: datetime(2026, 7, 26, 20, 15, tzinfo=timezone.utc)
    )

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


def test_tts_uid_canary_does_not_break_unattributed_legacy_callers(monkeypatch):
    monkeypatch.setenv("ELLA_AI_CONSENT_ENFORCEMENT_UIDS", "user-a")
    monkeypatch.delenv("ELLA_AI_CONSENT_ENFORCEMENT_ENABLED", raising=False)
    monkeypatch.delenv("ELLA_INTERNAL_VOICE_TTS_TOKEN", raising=False)

    result = consent.require_current_ai_consent_or_internal_tts(
        authorization=None,
        x_internal_token=None,
        x_subject_uid=None,
    )

    assert result == "migration-bypass"


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
    monkeypatch.setattr(consent.auth, "get_current_user_uid", lambda _authorization: "user-a")
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
        ai_consent.submit_ai_consent(request, uid="user-a")

    assert error.value.status_code == 409
    assert error.value.detail["code"] == "ai_consent_policy_mismatch"


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

    app.dependency_overrides[consent.auth.get_current_user_uid] = lambda: "user-a"
    status_response = client.get("/v1/users/ai-consent")
    assert status_response.status_code == 200
    assert status_response.json()["subject_uid"] == "user-a"
