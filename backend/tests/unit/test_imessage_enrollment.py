import asyncio
import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database.imessage_enrollment import ImessageAuthorityError, ImessageRuntimeSnapshot
from ella.routers import imessage_enrollment as enrollment_router
from ella.services import imessage_enrollment as enrollment_service
from ella.services.imessage_enrollment import (
    CONSENT_POLICY_VERSION,
    CONSENT_PROCESSOR_SET_HASH,
    CONSENT_SCOPE_HASH,
    CONSENT_SCOPE_VERSION,
    ImessageEnrollmentError,
    ImessageEnrollmentService,
    RegistrarCleanupResult,
    RegistrarError,
    RegistrarResult,
    consent_policy,
)
from utils.ella.exact_firebase_auth import get_exact_firebase_uid

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
USER_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
BINDING_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
TARGET_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")
ATTEMPT_ID = uuid.UUID("44444444-4444-4444-8444-444444444444")
REQUEST_ID = uuid.UUID("55555555-5555-4555-8555-555555555555")
CONSENT_ID = uuid.UUID("66666666-6666-4666-8666-666666666666")
IDEMPOTENCY_KEY = uuid.UUID("77777777-7777-4777-8777-777777777777")
TRANSPORT_TOKEN = "t" * 32


def _snapshot() -> ImessageRuntimeSnapshot:
    return ImessageRuntimeSnapshot(
        uid="owner-a",
        binding_id=BINDING_ID,
        target_id=TARGET_ID,
        authority_kind="target",
        authority_digest="a" * 64,
        binding_revision=3,
        entitlement_revision=4,
        account_user_id=USER_ID,
        profile_user_id=USER_ID,
    )


def _attempt(*, state: str = "prepared") -> dict:
    return {
        "id": ATTEMPT_ID,
        "state": state,
        "provider_request_id": REQUEST_ID,
        "created_at": NOW,
        "provider_registration_ref_hmac": "b" * 64 if state == "provider_accepted" else None,
        "assigned_destination_e164": "+15555550100" if state == "provider_accepted" else None,
        "assigned_destination_ref_hmac": "c" * 64 if state == "provider_accepted" else None,
    }


def _binding(*, status: str = "verification_pending") -> dict:
    return {
        "id": uuid.UUID("88888888-8888-4888-8888-888888888888"),
        "status": status,
        "generation": 1,
        "revision": 1,
        "assigned_destination_e164": "+15555550100",
        "challenge_expires_at": NOW + timedelta(minutes=15),
        "verified_at": NOW if status == "active" else None,
    }


class FakeRepository:
    def __init__(self, events=None):
        self.events = events if events is not None else []
        self.attempt = _attempt()
        self.binding = _binding()
        self.finalize_error = None
        self.state = {
            "user_status": "ACTIVE",
            "consent_decision": "granted",
            "binding_status": None,
        }
        self.deletion_fence = {
            "request_id": uuid.UUID("99999999-9999-4999-8999-999999999999"),
            "state": "pending",
            "provider_request_ids": [REQUEST_ID],
        }

    async def assert_schema_ready(self):
        self.events.append(("schema", {}))

    async def submit_consent(self, **kwargs):
        self.events.append(("consent", kwargs))
        submission = kwargs["submission"]
        return {
            "id": CONSENT_ID,
            "decision": submission.decision,
            "policy_version": submission.policy_version,
            "processor_set_hash": submission.processor_set_hash,
            "scope_version": submission.scope_version,
            "scope_hash": submission.scope_hash,
            "authority_revision": 1,
            "decided_at": NOW,
        }

    async def get_owner_state(self, **kwargs):
        self.events.append(("state", kwargs))
        return dict(self.state)

    async def prepare_registration(self, **kwargs):
        self.events.append(("prepare", kwargs))
        return dict(self.attempt), True

    async def mark_provider_accepted(self, **kwargs):
        self.events.append(("provider_accepted", kwargs))
        self.attempt = {
            **self.attempt,
            "state": "provider_accepted",
            "provider_registration_ref_hmac": kwargs["provider_registration_ref_hmac"],
            "assigned_destination_e164": kwargs["assigned_destination_e164"],
            "assigned_destination_ref_hmac": kwargs["assigned_destination_ref_hmac"],
        }
        return dict(self.attempt)

    async def finalize_registration(self, **kwargs):
        self.events.append(("finalize", kwargs))
        if self.finalize_error:
            raise self.finalize_error
        return dict(self.binding), True

    async def mark_registration_uncertain(self, **kwargs):
        self.events.append(("uncertain", kwargs))
        self.attempt = {**self.attempt, "state": "uncertain"}

    async def mark_registration_failed(self, **kwargs):
        self.events.append(("failed", kwargs))

    async def get_binding_for_attempt(self, **kwargs):
        self.events.append(("binding_for_attempt", kwargs))
        return dict(self.binding)

    async def retire_expired_pending_binding(self, **kwargs):
        self.events.append(("retire_expired", kwargs))
        self.binding = {**self.binding, "status": "quarantined", "revision": 2}
        self.attempt = {**self.attempt, "state": "quarantined"}
        return dict(self.binding)

    async def verify_inbound_proof(self, **kwargs):
        self.events.append(("verify_proof", kwargs))
        return dict(self.binding)

    async def resolve_proof_authority(self, **kwargs):
        self.events.append(("proof_authority", kwargs))
        return {
            "omi_uid": "owner-a",
            "runtime_binding_id": BINDING_ID,
            "runtime_target_id": TARGET_ID,
            "runtime_authority_digest": "a" * 64,
            "challenge_salt": "0" * 32,
        }

    async def revoke_binding(self, **kwargs):
        self.events.append(("revoke", kwargs))
        return {
            **self.binding,
            "status": "revoked",
            "generation": 2,
            "provider_request_id": REQUEST_ID,
        }

    async def provider_request_ids_for_owner(self, **kwargs):
        self.events.append(("provider_request_ids", kwargs))
        return [REQUEST_ID]

    async def begin_account_deletion_cleanup(self, **kwargs):
        self.events.append(("deletion_cleanup_begin", kwargs))
        return dict(self.deletion_fence)

    async def complete_account_deletion_cleanup(self, **kwargs):
        self.events.append(("deletion_cleanup_complete", kwargs))
        self.deletion_fence["state"] = "cleaned"
        return dict(self.deletion_fence)


class FakeRegistrar:
    def __init__(self, events, *, error=None):
        self.events = events
        self.error = error

    async def register(self, **kwargs):
        self.events.append(("register", kwargs))
        if self.error:
            raise self.error
        return RegistrarResult(
            registration_ref="provider-registration-a",
            assigned_destination="+15555550100",
        )

    async def cleanup(self, **kwargs):
        self.events.append(("cleanup", kwargs))
        if self.error:
            raise self.error
        return RegistrarCleanupResult(
            provider_disposition="provider_user_retained_unbound",
            operator_action_required=True,
        )


def _service(repository, registrar) -> ImessageEnrollmentService:
    return ImessageEnrollmentService(
        repository=repository,
        registrar=registrar,
        now=lambda: NOW,
        hmac_key=b"h" * 32,
        proof_key=b"p" * 32,
    )


def test_consent_policy_is_dedicated_text_dm_disclosure():
    assert consent_policy() == {
        "policy_version": CONSENT_POLICY_VERSION,
        "processor_set_hash": CONSENT_PROCESSOR_SET_HASH,
        "scope_version": CONSENT_SCOPE_VERSION,
        "scope_hash": CONSENT_SCOPE_HASH,
        "recipients": [
            "Ella self-hosted Hermes and Honcho",
            "Photon iMessage transport",
        ],
        "data_classes": [
            "your handset phone number used for iMessage transport registration",
            "the text messages you send to Ella",
            "Ella's text replies",
            "messaging delivery identifiers",
        ],
        "text_dm_only": True,
    }


def test_revoke_requires_exact_local_cleanup_and_reports_retained_provider_user(monkeypatch):
    monkeypatch.setenv("ELLA_IMESSAGE_ENROLLMENT_ENABLED", "true")
    repository = FakeRepository()
    registrar = FakeRegistrar(repository.events)
    service = _service(repository, registrar)

    result = asyncio.run(
        service.revoke(
            uid="owner-a",
            expected_generation=1,
            idempotency_key=IDEMPOTENCY_KEY,
        )
    )

    assert result["state"] == "revoked"
    assert result["cleanup"] == {
        "local_absence_proven": True,
        "provider_disposition": "provider_user_retained_unbound",
        "operator_action_required": True,
    }
    assert [event[0] for event in repository.events] == ["schema", "revoke", "cleanup"]
    assert repository.events[-1][1] == {"provider_request_id": str(REQUEST_ID)}


def test_revoke_stays_typed_unavailable_when_local_cleanup_is_ambiguous(monkeypatch):
    monkeypatch.setenv("ELLA_IMESSAGE_ENROLLMENT_ENABLED", "true")
    repository = FakeRepository()
    registrar = FakeRegistrar(
        repository.events,
        error=RegistrarError("imessage_registrar_cleanup_transport_uncertain", ambiguous=True),
    )
    service = _service(repository, registrar)

    with pytest.raises(ImessageEnrollmentError) as failure:
        asyncio.run(
            service.revoke(
                uid="owner-a",
                expected_generation=1,
                idempotency_key=IDEMPOTENCY_KEY,
            )
        )

    assert failure.value.code == "imessage_local_cleanup_uncertain"
    assert failure.value.status_code == 503
    assert [event[0] for event in repository.events] == ["schema", "revoke", "cleanup"]


def test_previous_policy_consent_is_rejected_before_authority_write(monkeypatch):
    monkeypatch.setenv("ELLA_IMESSAGE_ENROLLMENT_ENABLED", "true")
    repository = FakeRepository()
    service = _service(repository, FakeRegistrar(repository.events))

    with pytest.raises(ImessageEnrollmentError) as failure:
        asyncio.run(
            service.submit_consent(
                uid="owner-a",
                decision="granted",
                policy_version="ella-imessage-data-v1",
                processor_set_hash=CONSENT_PROCESSOR_SET_HASH,
                scope_version=CONSENT_SCOPE_VERSION,
                scope_hash=CONSENT_SCOPE_HASH,
                request_id=REQUEST_ID,
                app_version="1.0",
                build_number="1",
            )
        )

    assert CONSENT_POLICY_VERSION == "ella-imessage-data-v2"
    assert failure.value.code == "imessage_consent_policy_mismatch"
    assert failure.value.status_code == 409
    assert repository.events == [("schema", {})]


def test_feature_flag_defaults_off_before_repository_or_provider(monkeypatch):
    monkeypatch.delenv("ELLA_IMESSAGE_ENROLLMENT_ENABLED", raising=False)
    repository = FakeRepository()
    registrar = FakeRegistrar(repository.events)
    service = _service(repository, registrar)

    status = asyncio.run(service.status(uid="owner-a"))

    assert status["state"] == "temporarily_unavailable"
    assert status["reason_code"] == "rollout_disabled"
    assert repository.events == []
    with pytest.raises(ImessageEnrollmentError, match="imessage_enrollment_disabled"):
        asyncio.run(
            service.start(
                uid="owner-a",
                handset_e164="+15555550123",
                consent_receipt_id=CONSENT_ID,
                idempotency_key=IDEMPOTENCY_KEY,
            )
        )
    assert repository.events == []


def test_start_persists_before_provider_and_revalidates_before_finalization(monkeypatch):
    monkeypatch.setenv("ELLA_IMESSAGE_ENROLLMENT_ENABLED", "true")
    events = []
    repository = FakeRepository(events)
    service = _service(repository, FakeRegistrar(events))
    identity = SimpleNamespace(uid="owner-a", target_mode="hermes-chat", digest="a" * 64)
    service._runtime = AsyncMock(return_value=(SimpleNamespace(), _snapshot(), identity))

    async def revalidate(observed):
        assert observed is identity
        events.append(("revalidate", {}))

    monkeypatch.setattr(enrollment_service, "revalidate_runtime_authority", revalidate)

    body, created = asyncio.run(
        service.start(
            uid="owner-a",
            handset_e164="+15555550123",
            consent_receipt_id=CONSENT_ID,
            idempotency_key=IDEMPOTENCY_KEY,
        )
    )

    assert created is True
    assert [event[0] for event in events] == [
        "schema",
        "prepare",
        "register",
        "provider_accepted",
        "revalidate",
        "finalize",
    ]
    assert set(events[2][1]) == {"handset_e164", "provider_request_id"}
    assert body["status"]["state"] == "verification_pending"
    assert len(body["proof"]["code"]) == 6


def test_provider_accepted_retry_skips_second_external_registration(monkeypatch):
    monkeypatch.setenv("ELLA_IMESSAGE_ENROLLMENT_ENABLED", "true")
    events = []
    repository = FakeRepository(events)
    repository.attempt = _attempt(state="provider_accepted")
    registrar = FakeRegistrar(events)
    service = _service(repository, registrar)
    identity = SimpleNamespace(uid="owner-a", target_mode="hermes-chat", digest="a" * 64)
    service._runtime = AsyncMock(return_value=(SimpleNamespace(), _snapshot(), identity))

    async def revalidate(observed):
        assert observed is identity
        events.append(("revalidate", {}))

    monkeypatch.setattr(enrollment_service, "revalidate_runtime_authority", revalidate)

    asyncio.run(
        service.start(
            uid="owner-a",
            handset_e164="+15555550123",
            consent_receipt_id=CONSENT_ID,
            idempotency_key=IDEMPOTENCY_KEY,
        )
    )

    assert [event[0] for event in events] == ["schema", "prepare", "revalidate", "finalize"]


def test_ambiguous_provider_result_is_durable_and_never_finalized(monkeypatch):
    monkeypatch.setenv("ELLA_IMESSAGE_ENROLLMENT_ENABLED", "true")
    events = []
    repository = FakeRepository(events)
    registrar = FakeRegistrar(
        events,
        error=RegistrarError("imessage_registrar_transport_uncertain", ambiguous=True),
    )
    service = _service(repository, registrar)
    service._runtime = AsyncMock(
        return_value=(
            SimpleNamespace(),
            _snapshot(),
            SimpleNamespace(uid="owner-a", target_mode="hermes-chat", digest="a" * 64),
        )
    )

    with pytest.raises(ImessageEnrollmentError, match="imessage_registrar_transport_uncertain"):
        asyncio.run(
            service.start(
                uid="owner-a",
                handset_e164="+15555550123",
                consent_receipt_id=CONSENT_ID,
                idempotency_key=IDEMPOTENCY_KEY,
            )
        )

    assert [event[0] for event in events] == ["schema", "prepare", "register", "uncertain"]


def test_provider_accepted_finalization_drift_is_quarantined_without_repeat_registration(monkeypatch):
    monkeypatch.setenv("ELLA_IMESSAGE_ENROLLMENT_ENABLED", "true")
    events = []
    repository = FakeRepository(events)
    repository.finalize_error = ImessageAuthorityError("imessage_consent_authority_changed")
    registrar = FakeRegistrar(events)
    service = _service(repository, registrar)
    service._runtime = AsyncMock(
        return_value=(
            SimpleNamespace(),
            _snapshot(),
            SimpleNamespace(uid="owner-a", target_mode="hermes-chat", digest="a" * 64),
        )
    )
    monkeypatch.setattr(enrollment_service, "revalidate_runtime_authority", AsyncMock())

    with pytest.raises(ImessageEnrollmentError) as failure:
        asyncio.run(
            service.start(
                uid="owner-a",
                handset_e164="+15555550123",
                consent_receipt_id=CONSENT_ID,
                idempotency_key=IDEMPOTENCY_KEY,
            )
        )

    assert failure.value.code == "imessage_consent_authority_changed"
    assert failure.value.status_code == 409
    assert repository.attempt["state"] == "uncertain"
    assert [event[0] for event in events] == [
        "schema",
        "prepare",
        "register",
        "provider_accepted",
        "finalize",
        "uncertain",
    ]

    events.clear()
    with pytest.raises(ImessageEnrollmentError, match="imessage_registration_manual_reconciliation_required"):
        asyncio.run(
            service.start(
                uid="owner-a",
                handset_e164="+15555550123",
                consent_receipt_id=CONSENT_ID,
                idempotency_key=uuid.uuid4(),
            )
        )
    assert [event[0] for event in events] == ["schema", "prepare"]


def test_inbound_proof_hashes_transport_values_and_never_accepts_owner_selector(monkeypatch):
    monkeypatch.setenv("ELLA_IMESSAGE_ENROLLMENT_ENABLED", "true")
    repository = FakeRepository()
    service = _service(repository, FakeRegistrar(repository.events))
    service._runtime = AsyncMock(
        return_value=(
            SimpleNamespace(),
            _snapshot(),
            SimpleNamespace(uid="owner-a", target_mode="hermes-chat", digest="a" * 64),
        )
    )

    receipt = asyncio.run(
        service.verify_proof(
            assigned_destination="+15555550100",
            handset_e164="+15555550123",
            code="123456",
            provider_message_id="provider-message-a",
            line_identity="line-a",
            contact_identity="contact-a",
        )
    )

    assert repository.events[-2][0] == "proof_authority"
    call = repository.events[-1]
    assert call[0] == "verify_proof"
    assert set(call[1]) == {
        "assigned_destination_ref_hmac",
        "handset_ref_hmac",
        "line_identity_hmac",
        "contact_identity_hmac",
        "provider_message_ref_hmac",
        "candidate_challenge_hash",
        "consent_contract",
        "runtime",
        "now",
    }
    assert all(len(value) == 64 for key, value in call[1].items() if key.endswith("_hmac"))
    assert call[1]["candidate_challenge_hash"] == service._challenge_hash(salt="0" * 32, code="123456")
    assert call[1]["candidate_challenge_hash"] != hashlib.sha256(f'{"0" * 32}:123456'.encode()).hexdigest()
    assert receipt["status"] == "accepted"


def test_status_requires_the_current_consent_contract(monkeypatch):
    monkeypatch.setenv("ELLA_IMESSAGE_ENROLLMENT_ENABLED", "true")
    repository = FakeRepository()
    repository.state.update(
        {
            "policy_version": "stale-policy",
            "processor_set_hash": CONSENT_PROCESSOR_SET_HASH,
            "scope_version": CONSENT_SCOPE_VERSION,
            "scope_hash": CONSENT_SCOPE_HASH,
        }
    )
    service = _service(repository, FakeRegistrar(repository.events))

    status = asyncio.run(service.status(uid="owner-a"))

    assert status["state"] == "not_connected"
    assert status["reason_code"] == "consent_policy_stale"
    assert [event[0] for event in repository.events] == ["schema", "state"]


def test_status_atomically_retires_expired_pending_challenge(monkeypatch):
    monkeypatch.setenv("ELLA_IMESSAGE_ENROLLMENT_ENABLED", "true")
    repository = FakeRepository()
    repository.binding = _binding()
    repository.binding["challenge_expires_at"] = NOW - timedelta(seconds=1)
    repository.state.update(
        {
            "policy_version": CONSENT_POLICY_VERSION,
            "processor_set_hash": CONSENT_PROCESSOR_SET_HASH,
            "scope_version": CONSENT_SCOPE_VERSION,
            "scope_hash": CONSENT_SCOPE_HASH,
            "binding_id": repository.binding["id"],
            "binding_status": "verification_pending",
            "generation": 1,
            "binding_revision": 1,
            "assigned_destination_e164": repository.binding["assigned_destination_e164"],
            "challenge_expires_at": repository.binding["challenge_expires_at"],
        }
    )
    service = _service(repository, FakeRegistrar(repository.events))

    status = asyncio.run(service.status(uid="owner-a"))

    assert status["state"] == "temporarily_unavailable"
    assert status["reason_code"] == "binding_quarantined"
    assert status["verification_expires_at"] == repository.binding["challenge_expires_at"].isoformat()
    assert [event[0] for event in repository.events] == [
        "schema",
        "state",
        "retire_expired",
        "provider_request_ids",
        "cleanup",
    ]


def test_consent_authority_conflicts_use_documented_http_409_semantics():
    for code in (
        "imessage_consent_required",
        "imessage_consent_receipt_stale",
        "imessage_consent_authority_changed",
        "imessage_consent_policy_stale",
    ):
        mapped = ImessageEnrollmentService._authority_error(ImessageAuthorityError(code))
        assert mapped.code == code
        assert mapped.status_code == 409


def test_finalized_retry_does_not_reissue_proof_for_revoked_binding(monkeypatch):
    monkeypatch.setenv("ELLA_IMESSAGE_ENROLLMENT_ENABLED", "true")
    repository = FakeRepository()
    repository.attempt = _attempt(state="finalized")
    repository.binding = _binding(status="revoked")
    service = _service(repository, FakeRegistrar(repository.events))
    service._runtime = AsyncMock(
        return_value=(
            SimpleNamespace(),
            _snapshot(),
            SimpleNamespace(uid="owner-a", target_mode="hermes-chat", digest="a" * 64),
        )
    )

    with pytest.raises(ImessageEnrollmentError, match="imessage_registration_state_invalid"):
        asyncio.run(
            service.start(
                uid="owner-a",
                handset_e164="+15555550123",
                consent_receipt_id=CONSENT_ID,
                idempotency_key=IDEMPOTENCY_KEY,
            )
        )

    assert [event[0] for event in repository.events] == ["schema", "prepare", "binding_for_attempt"]


def test_finalized_retry_retires_expired_proof_instead_of_reissuing(monkeypatch):
    monkeypatch.setenv("ELLA_IMESSAGE_ENROLLMENT_ENABLED", "true")
    repository = FakeRepository()
    repository.attempt = _attempt(state="finalized")
    repository.binding = _binding()
    repository.binding["challenge_expires_at"] = NOW - timedelta(seconds=1)
    service = _service(repository, FakeRegistrar(repository.events))
    service._runtime = AsyncMock(
        return_value=(
            SimpleNamespace(),
            _snapshot(),
            SimpleNamespace(uid="owner-a", target_mode="hermes-chat", digest="a" * 64),
        )
    )

    with pytest.raises(ImessageEnrollmentError) as failure:
        asyncio.run(
            service.start(
                uid="owner-a",
                handset_e164="+15555550123",
                consent_receipt_id=CONSENT_ID,
                idempotency_key=IDEMPOTENCY_KEY,
            )
        )

    assert failure.value.code == "imessage_registration_proof_window_expired"
    assert failure.value.status_code == 409
    assert repository.binding["status"] == "quarantined"
    assert [event[0] for event in repository.events] == [
        "schema",
        "prepare",
        "binding_for_attempt",
        "retire_expired",
        "provider_request_ids",
        "cleanup",
    ]


def test_consent_withdrawal_revokes_then_proves_exact_local_cleanup(monkeypatch):
    monkeypatch.setenv("ELLA_IMESSAGE_ENROLLMENT_ENABLED", "true")
    events = []
    repository = FakeRepository(events)
    service = _service(repository, FakeRegistrar(events))

    receipt = asyncio.run(
        service.submit_consent(
            uid="owner-a",
            decision="revoked",
            policy_version=CONSENT_POLICY_VERSION,
            processor_set_hash=CONSENT_PROCESSOR_SET_HASH,
            scope_version=CONSENT_SCOPE_VERSION,
            scope_hash=CONSENT_SCOPE_HASH,
            request_id=REQUEST_ID,
            app_version="1.0",
            build_number="1",
        )
    )

    assert receipt["decision"] == "revoked"
    assert [event[0] for event in events] == [
        "schema",
        "consent",
        "provider_request_ids",
        "cleanup",
    ]


def test_account_deletion_cleanup_fences_before_exact_local_absence_proof():
    events = []
    repository = FakeRepository(events)
    registrar = FakeRegistrar(events)
    service = _service(repository, registrar)

    result = asyncio.run(service.cleanup_for_account_deletion(uid="owner-a"))

    assert result == {
        "local_absence_proven": True,
        "provider_disposition": "provider_user_retained_unbound",
        "operator_action_required": True,
    }
    assert [event[0] for event in events] == [
        "schema",
        "deletion_cleanup_begin",
        "cleanup",
        "deletion_cleanup_complete",
    ]
    assert events[2][1] == {"provider_request_id": str(REQUEST_ID)}


def test_account_deletion_cleanup_failure_keeps_durable_fence_pending():
    events = []
    repository = FakeRepository(events)
    registrar = FakeRegistrar(events, error=RegistrarError("cleanup unavailable", ambiguous=True))
    service = _service(repository, registrar)

    with pytest.raises(ImessageEnrollmentError) as failure:
        asyncio.run(service.cleanup_for_account_deletion(uid="owner-a"))

    assert failure.value.code == "imessage_account_cleanup_uncertain"
    assert repository.deletion_fence["state"] == "pending"
    assert [event[0] for event in events] == ["schema", "deletion_cleanup_begin", "cleanup"]


class RouteService:
    def __init__(self):
        self.calls = []

    async def submit_consent(self, **kwargs):
        self.calls.append(("consent", kwargs))
        return {"ok": True}

    async def status(self, **kwargs):
        self.calls.append(("status", kwargs))
        return {"state": "not_connected"}

    async def start(self, **kwargs):
        self.calls.append(("start", kwargs))
        return {"status": {}, "proof": {}}, True

    async def revoke(self, **kwargs):
        self.calls.append(("revoke", kwargs))
        return {"state": "revoked"}

    async def verify_proof(self, **kwargs):
        self.calls.append(("proof", kwargs))
        return {"status": "accepted"}


def _route_client(service, *, authenticated=True):
    app = FastAPI()
    app.include_router(enrollment_router.router)
    app.dependency_overrides[enrollment_router.get_imessage_enrollment_service] = lambda: service
    if authenticated:
        app.dependency_overrides[get_exact_firebase_uid] = lambda: "owner-a"
    return TestClient(app)


def test_mounted_owner_routes_reject_caller_uid_and_policy_is_public():
    service = RouteService()
    client = _route_client(service)

    policy = client.get("/v1/ella/imessage/consent/policy")
    response = client.post(
        "/v1/ella/imessage/enrollment/start",
        json={
            "uid": "owner-b",
            "handset_e164": "+15555550123",
            "consent_receipt_id": str(CONSENT_ID),
            "idempotency_key": str(IDEMPOTENCY_KEY),
        },
    )

    assert policy.status_code == 200
    assert policy.json()["scope_version"] == CONSENT_SCOPE_VERSION
    assert response.status_code == 422
    assert service.calls == []


def test_mounted_owner_status_requires_firebase_before_service():
    service = RouteService()
    client = _route_client(service, authenticated=False)

    response = client.get("/v1/ella/imessage/enrollment")

    assert response.status_code == 401
    assert service.calls == []


@pytest.mark.parametrize(
    ("configured", "provided", "expected"),
    [
        (None, None, 503),
        (TRANSPORT_TOKEN, None, 403),
        (TRANSPORT_TOKEN, "", 403),
        (TRANSPORT_TOKEN, "wrong", 403),
        (" " * 32, " " * 32, 503),
    ],
)
def test_mounted_transport_proof_denies_before_service(monkeypatch, configured, provided, expected):
    if configured is None:
        monkeypatch.delenv("ELLA_IMESSAGE_TRANSPORT_TOKEN", raising=False)
    else:
        monkeypatch.setenv("ELLA_IMESSAGE_TRANSPORT_TOKEN", configured)
    service = RouteService()
    client = _route_client(service)
    headers = {"X-Ella-Imessage-Transport-Token": provided} if provided is not None else {}

    response = client.post(
        "/v1/ella/internal/imessage/proof",
        headers=headers,
        json={
            "assigned_destination": "+15555550100",
            "handset_e164": "+15555550123",
            "code": "123456",
            "provider_message_id": "message-a",
            "line_identity": "line-a",
            "contact_identity": "contact-a",
        },
    )

    assert response.status_code == expected
    assert service.calls == []


def test_mounted_transport_proof_has_no_uid_selector(monkeypatch):
    monkeypatch.setenv("ELLA_IMESSAGE_TRANSPORT_TOKEN", TRANSPORT_TOKEN)
    service = RouteService()
    client = _route_client(service)

    response = client.post(
        "/v1/ella/internal/imessage/proof",
        headers={"X-Ella-Imessage-Transport-Token": TRANSPORT_TOKEN},
        json={
            "uid": "owner-b",
            "assigned_destination": "+15555550100",
            "handset_e164": "+15555550123",
            "code": "123456",
            "provider_message_id": "message-a",
            "line_identity": "line-a",
            "contact_identity": "contact-a",
        },
    )

    assert response.status_code == 422
    assert service.calls == []


def test_mounted_transport_proof_accepts_configured_transport_without_owner_selector(monkeypatch):
    monkeypatch.setenv("ELLA_IMESSAGE_TRANSPORT_TOKEN", TRANSPORT_TOKEN)
    service = RouteService()
    client = _route_client(service)

    response = client.post(
        "/v1/ella/internal/imessage/proof",
        headers={"X-Ella-Imessage-Transport-Token": TRANSPORT_TOKEN},
        json={
            "assigned_destination": "+15555550100",
            "handset_e164": "+15555550123",
            "code": "123456",
            "provider_message_id": "message-a",
            "line_identity": "line-a",
            "contact_identity": "contact-a",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}
    assert service.calls[0][0] == "proof"
    assert "uid" not in service.calls[0][1]


def test_mounted_enrollment_surfaces_are_no_store_including_start_json_response(monkeypatch):
    monkeypatch.setenv("ELLA_IMESSAGE_TRANSPORT_TOKEN", TRANSPORT_TOKEN)
    service = RouteService()
    client = _route_client(service)

    responses = [
        client.get("/v1/ella/imessage/consent/policy"),
        client.post(
            "/v1/ella/imessage/consent",
            json={
                "decision": "granted",
                "policy_version": CONSENT_POLICY_VERSION,
                "processor_set_hash": CONSENT_PROCESSOR_SET_HASH,
                "scope_version": CONSENT_SCOPE_VERSION,
                "scope_hash": CONSENT_SCOPE_HASH,
                "request_id": str(REQUEST_ID),
                "app_version": "1.0",
                "build_number": "1",
            },
        ),
        client.get("/v1/ella/imessage/enrollment"),
        client.post(
            "/v1/ella/imessage/enrollment/start",
            json={
                "handset_e164": "+15555550123",
                "consent_receipt_id": str(CONSENT_ID),
                "idempotency_key": str(IDEMPOTENCY_KEY),
            },
        ),
        client.post(
            "/v1/ella/imessage/enrollment/revoke",
            json={"expected_generation": 1, "idempotency_key": str(IDEMPOTENCY_KEY)},
        ),
        client.post(
            "/v1/ella/internal/imessage/proof",
            headers={"X-Ella-Imessage-Transport-Token": TRANSPORT_TOKEN},
            json={
                "assigned_destination": "+15555550100",
                "handset_e164": "+15555550123",
                "code": "123456",
                "provider_message_id": "message-a",
                "line_identity": "line-a",
                "contact_identity": "contact-a",
            },
        ),
    ]

    assert [response.status_code for response in responses] == [200, 200, 200, 201, 200, 200]
    assert all(response.headers.get("cache-control") == "no-store" for response in responses)


def test_registrar_rejects_non_tls_non_loopback_authority():
    with pytest.raises(RegistrarError, match="imessage_registrar_not_configured"):
        enrollment_service.PhotonRegistrarClient(
            base_url="http://example.test",
            token="r" * 32,
        )


def test_equal_binding_and_proof_keys_fail_closed(monkeypatch):
    monkeypatch.setenv("ELLA_IMESSAGE_ENROLLMENT_ENABLED", "true")
    repository = FakeRepository()
    service = ImessageEnrollmentService(
        repository=repository,
        registrar=FakeRegistrar(repository.events),
        hmac_key=b"s" * 32,
        proof_key=b"s" * 32,
    )

    with pytest.raises(ImessageEnrollmentError, match="imessage_enrollment_key_unavailable"):
        asyncio.run(service.status(uid="owner-a"))
    assert repository.events == []
