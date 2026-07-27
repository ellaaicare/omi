import asyncio
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database.ella_provisioning import RuntimePoolClaimError
from ella.routers.photon import create_photon_router
from ella.routers.canonical_events import CanonicalEventIn, InMemoryCanonicalEventStore
from ella.services.hermes_cloud import HermesCloudPreflight
from ella.services.hermes_cloud_photon import (
    PHOTON_ALLOWED_REGULAR_COMMANDS,
    PHOTON_ALLOWED_TOOLS,
    PHOTON_COMMAND_TIER_VERSION,
    HermesCloudPhotonAdapter,
    PhotonAdapterConfig,
    PhotonDeliveryAck,
    PhotonInboundEnvelope,
    PhotonSidecarPreflight,
)
from ella.services.hermes_cloud_runtime import HermesCloudTurnResult
from ella.services.runtime_errors import ProvisioningError
from ella.services.runtime_resolver import IsolatedRuntime

POLICY_SHA = "a" * 40
MANIFEST_SHA = "b" * 64
NOW = datetime.now(timezone.utc)
IDENTITY_HMAC_KEY = "identity-key-" + ("x" * 32)
PROVIDER_MESSAGE_ID = "provider-message-raw"


class SimulatedProcessCrash(BaseException):
    pass


def _opaque_key(namespace: str, raw_value: str) -> str:
    return hmac.new(
        IDENTITY_HMAC_KEY.encode("utf-8"),
        f"{namespace}\x1f{raw_value}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _config(*, authorize_synthetic_fixture: bool = True) -> PhotonAdapterConfig:
    return PhotonAdapterConfig(
        enabled=True,
        internal_owner_uid="synthetic-owner",
        identity_hmac_key=IDENTITY_HMAC_KEY,
        approved_policy_commit_sha=POLICY_SHA,
        preflight_max_age_seconds=30,
        synthetic_message_keys=(
            frozenset({_opaque_key("photon-inbound-message", PROVIDER_MESSAGE_ID)})
            if authorize_synthetic_fixture
            else frozenset()
        ),
    )


def _runtime(**updates) -> IsolatedRuntime:
    values = dict(
        uid="synthetic-owner",
        binding_id="00000000-0000-0000-0000-000000000001",
        provider="hermes_cloud",
        status="internal_canary",
        profile_name="owner-profile",
        agent_id="hermes-cloud",
        runtime_instance_id="instance-a",
        gateway_url="https://cloud.example.test",
        gateway_token="server-secret",
        workspace_root="",
        honcho_workspace="workspace-a",
        observed_peer="user-a",
        observer_peer="companion-a",
        prompt_pack_version="prompt-v1",
        expected_model="gpt-5.6-terra",
        model_context_window_tokens=262144,
        allowed_tools=PHOTON_ALLOWED_TOOLS,
        required_capabilities=("responses_api", "session_key_header"),
        model_policy_version="ella-hermes-cloud-v1",
        voice_policy_version="voice-v1",
        revision=7,
        policy_commit_sha=POLICY_SHA,
        approval_manifest_sha256=MANIFEST_SHA,
    )
    values.update(updates)
    return IsolatedRuntime(**values)


class FakeRepository:
    def __init__(self, config: PhotonAdapterConfig):
        self.config = config
        line_key = config.opaque_key("photon-line", "line-raw")
        contact_key = config.opaque_key("photon-contact", "contact-raw")
        self.binding = {
            "id": "00000000-0000-0000-0000-000000000001",
            "photon_binding_id": "00000000-0000-0000-0000-000000000002",
            "omi_uid": "synthetic-owner",
            "photon_role": "internal-owner",
            "photon_status": "enabled",
            "provider": "hermes_cloud",
            "status": "internal_canary",
            "active": True,
            "health_state": "healthy",
            "line_identity_key": line_key,
            "contact_identity_key": contact_key,
            "photon_policy_commit_sha": POLICY_SHA,
            "command_tier_version": PHOTON_COMMAND_TIER_VERSION,
            "allow_all": False,
            "attachments_enabled": False,
            "caregiver_delivery_enabled": False,
            "rollout_phase": 3,
            "daily_message_limit": 100,
            "daily_initiation_limit": 10,
            "sidecar_connection_key": None,
            "sidecar_connected_at": None,
            "oauth_expires_at": None,
            "photon_preflight_receipt": {},
        }
        self.messages = {}
        self.claim_calls = []
        self.quota_calls = []
        self.failures = []
        self.ack_calls = []
        self.completed_runtime_receipts = set()
        self.crash_before_quota = False
        self.crash_before_provider_mark = False
        self.crash_on_complete = False

    async def resolve_photon_channel_binding(self, **kwargs):
        if (
            kwargs["line_identity_key"] != self.binding["line_identity_key"]
            or kwargs["contact_identity_key"] != self.binding["contact_identity_key"]
        ):
            return None
        return dict(self.binding)

    async def record_photon_sidecar_preflight(self, **kwargs):
        self.binding.update(
            sidecar_connection_key=kwargs["connection_key"],
            sidecar_connected_at=datetime.now(timezone.utc),
            oauth_expires_at=kwargs["oauth_expires_at"],
            photon_preflight_receipt=dict(kwargs["receipt"]),
        )
        return dict(self.binding)

    async def claim_photon_message(self, **kwargs):
        self.claim_calls.append(dict(kwargs))
        key = kwargs["inbound_provider_message_key"]
        existing = self.messages.get(key)
        if existing:
            if existing["inbound_payload_sha256"] != kwargs["inbound_payload_sha256"]:
                raise RuntimePoolClaimError("photon_duplicate_payload_conflict")
            stale = existing["status"] in {"claimed", "running"} and (
                not isinstance(existing.get("lease_expires_at"), datetime)
                or existing["lease_expires_at"] <= datetime.now(timezone.utc)
            )
            if stale and existing["status"] in {"claimed", "running"}:
                if existing["provider_started"] and existing["id"] not in self.completed_runtime_receipts:
                    existing.update(
                        status="uncertain",
                        reconciliation_status="manual_required",
                        error_code="photon_provider_outcome_unconfirmed",
                        lease_token=None,
                        lease_expires_at=None,
                    )
                    return {
                        **existing,
                        "inserted": False,
                        "reclaimed": False,
                        "acquired": False,
                    }
                existing.update(
                    lease_token=str(uuid.uuid4()),
                    lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=kwargs["lease_seconds"]),
                    attempt_count=existing["attempt_count"] + 1,
                    reconciliation_status="recovered",
                    error_code=None,
                )
                return {
                    **existing,
                    "inserted": False,
                    "reclaimed": True,
                    "acquired": True,
                }
            return {
                **existing,
                "inserted": False,
                "reclaimed": False,
                "acquired": False,
            }
        lease_token = str(uuid.uuid4())
        row = {
            "id": "00000000-0000-0000-0000-000000000003",
            "photon_binding_id": kwargs["photon_binding_id"],
            "inbound_provider_message_key": key,
            "inbound_payload_sha256": kwargs["inbound_payload_sha256"],
            "delivery_idempotency_key": "00000000-0000-0000-0000-000000000004",
            "status": "claimed",
            "quota_reserved": False,
            "writeback_receipt": {},
            "provider_started": False,
            "attempt_count": 1,
            "lease_token": lease_token,
            "lease_expires_at": datetime.now(timezone.utc) + timedelta(seconds=kwargs["lease_seconds"]),
            "reconciliation_status": "none",
        }
        self.messages[key] = row
        return {
            **row,
            "inserted": True,
            "reclaimed": False,
            "acquired": True,
        }

    async def reserve_photon_quota(self, **kwargs):
        if self.crash_before_quota:
            self.crash_before_quota = False
            raise SimulatedProcessCrash()
        self.quota_calls.append(dict(kwargs))
        row = next(item for item in self.messages.values() if item["id"] == kwargs["receipt_id"])
        if row["lease_token"] != kwargs["lease_token"]:
            raise RuntimePoolClaimError("photon_message_claim_conflict")
        if row["quota_reserved"]:
            return dict(row)
        row.update(status="running", quota_reserved=True)
        return dict(row)

    async def mark_photon_provider_started(self, **kwargs):
        if self.crash_before_provider_mark:
            self.crash_before_provider_mark = False
            raise SimulatedProcessCrash()
        row = next(item for item in self.messages.values() if item["id"] == kwargs["receipt_id"])
        if row["lease_token"] != kwargs["lease_token"]:
            raise RuntimePoolClaimError("photon_message_claim_conflict")
        row["provider_started"] = True
        return dict(row)

    async def complete_photon_message(self, **kwargs):
        if self.crash_on_complete:
            self.crash_on_complete = False
            raise SimulatedProcessCrash()
        row = next(item for item in self.messages.values() if item["id"] == kwargs["receipt_id"])
        if row["lease_token"] != kwargs["lease_token"]:
            raise RuntimePoolClaimError("photon_message_claim_conflict")
        row.update(
            status="awaiting_delivery",
            runtime_interaction_id=kwargs["runtime_interaction_id"],
            canonical_inbound_event_id=kwargs["canonical_inbound_event_id"],
            canonical_outbound_event_id=kwargs["canonical_outbound_event_id"],
            runtime_revision=kwargs["runtime_revision"],
            expected_model=kwargs["expected_model"],
            policy_commit_sha=kwargs["policy_commit_sha"],
            usage=dict(kwargs["usage"]),
            preflight_receipt=dict(kwargs["preflight_receipt"]),
            writeback_receipt=dict(kwargs["writeback_receipt"]),
            provider_started=True,
            lease_token=None,
            lease_expires_at=None,
        )
        return dict(row)

    async def fail_photon_message(self, **kwargs):
        self.failures.append(dict(kwargs))
        row = next(item for item in self.messages.values() if item["id"] == kwargs["receipt_id"])
        if row["status"] not in {"claimed", "running"} or row["lease_token"] != kwargs["lease_token"]:
            return
        row.update(
            status="uncertain" if kwargs["uncertain"] else "failed",
            error_code=kwargs["error_code"],
            provider_started=kwargs["provider_started"],
            reconciliation_status=("manual_required" if kwargs["uncertain"] else row["reconciliation_status"]),
            lease_token=None,
            lease_expires_at=None,
        )

    def expire_active_lease(self):
        row = next(iter(self.messages.values()))
        row["lease_expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)

    async def get_photon_message_receipt(self, **kwargs):
        for item in self.messages.values():
            if item["id"] == kwargs["receipt_id"]:
                return {
                    **item,
                    "photon_status": self.binding["photon_status"],
                    "sidecar_connection_key": self.binding["sidecar_connection_key"],
                    "oauth_expires_at": self.binding["oauth_expires_at"],
                }
        return None

    async def acknowledge_photon_delivery(self, **kwargs):
        self.ack_calls.append(dict(kwargs))
        row = next(item for item in self.messages.values() if item["id"] == kwargs["receipt_id"])
        if str(row["delivery_idempotency_key"]) != kwargs["delivery_idempotency_key"]:
            raise RuntimePoolClaimError("photon_delivery_ack_conflict")
        existing = row.get("outbound_provider_message_key")
        if existing and existing != kwargs["outbound_provider_message_key"]:
            raise RuntimePoolClaimError("photon_delivery_ack_conflict")
        row.update(
            status="delivered",
            outbound_provider_message_key=kwargs["outbound_provider_message_key"],
            delivery_receipt=dict(kwargs["delivery_receipt"]),
        )
        return dict(row)


class FakeCloudClient:
    def __init__(self, *, tools=PHOTON_ALLOWED_TOOLS):
        self.tools = tools
        self.calls = []

    async def preflight(self, binding):
        self.calls.append(dict(binding))
        return HermesCloudPreflight(
            model="gpt-5.6-terra",
            tools=tuple(self.tools),
            capabilities=("responses_api", "session_key_header"),
            receipt={"status": "ok", "content_free": True},
        )


class FakeRuntimeService:
    def __init__(self, event_store, repository, *, failure=None):
        self.event_store = event_store
        self.repository = repository
        self.failure = failure
        self.calls = []
        self.provider_calls = []
        self.completed = {}
        self.crash_after_provider_start = False

    async def run_turn(self, runtime, request):
        self.calls.append((runtime, request))
        existing = self.completed.get(request.client_interaction_id)
        if existing:
            return HermesCloudTurnResult(**{**existing.__dict__, "duplicate": True})
        if self.failure:
            raise self.failure
        self.provider_calls.append(request.client_interaction_id)
        if self.crash_after_provider_start:
            self.crash_after_provider_start = False
            raise SimulatedProcessCrash()
        source_identity = HermesCloudPhotonAdapter._source_identity(request.uid, request.client_interaction_id)
        inbound_event_id = "canonical-inbound"
        outbound_event_id = "canonical-outbound"
        await self.event_store.write_batch(
            [
                CanonicalEventIn(
                    uid=request.uid,
                    canonical_identity=request.uid,
                    event_id=outbound_event_id,
                    session_id="opaque-session",
                    channel="photon",
                    provider="hermes_cloud",
                    role="assistant",
                    text="Synthetic answer.",
                    started_at=NOW,
                    source_ref={"source_identity": source_identity},
                    metadata={"content_free_identifiers": True},
                )
            ]
        )
        result = HermesCloudTurnResult(
            text="Synthetic answer.",
            response_id="provider-response",
            canonical_user_event_id=inbound_event_id,
            canonical_assistant_event_id=outbound_event_id,
            duplicate=False,
            usage={"input_tokens": 10, "output_tokens": 4},
            runtime_interaction_id="00000000-0000-0000-0000-000000000005",
        )
        self.completed[request.client_interaction_id] = result
        receipt_id = request.client_interaction_id.removeprefix("photon:")
        self.repository.completed_runtime_receipts.add(receipt_id)
        return result


def _preflight() -> PhotonSidecarPreflight:
    return PhotonSidecarPreflight(
        line_identity="line-raw",
        contact_identity="contact-raw",
        connection_id="connection-raw",
        oauth_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        allow_all=False,
        allowed_contact_count=1,
        attachments_enabled=False,
        groups_enabled=False,
        command_tier_version=PHOTON_COMMAND_TIER_VERSION,
        allowed_regular_commands=PHOTON_ALLOWED_REGULAR_COMMANDS,
    )


def _message(**updates) -> PhotonInboundEnvelope:
    values = dict(
        line_identity="line-raw",
        contact_identity="contact-raw",
        connection_id="connection-raw",
        provider_message_id=PROVIDER_MESSAGE_ID,
        text="Synthetic hello.",
        occurred_at=NOW,
        conversation_initiation=True,
        synthetic=True,
    )
    values.update(updates)
    return PhotonInboundEnvelope(**values)


def _adapter(
    *,
    cloud=None,
    runtime_failure=None,
    authorize_synthetic_fixture=True,
):
    config = _config(
        authorize_synthetic_fixture=authorize_synthetic_fixture,
    )
    repository = FakeRepository(config)
    event_store = InMemoryCanonicalEventStore()
    runtime_service = FakeRuntimeService(
        event_store,
        repository,
        failure=runtime_failure,
    )

    async def resolve(uid, repository_arg):
        assert uid == "synthetic-owner"
        assert repository_arg is repository
        return _runtime()

    adapter = HermesCloudPhotonAdapter(
        repository=repository,
        event_store=event_store,
        config=config,
        cloud_client=cloud or FakeCloudClient(),
        runtime_resolver=resolve,
        runtime_service_factory=lambda: runtime_service,
    )
    return adapter, repository, runtime_service


def test_sidecar_preflight_turn_replay_and_delivery_ack_are_idempotent_and_opaque():
    adapter, repository, runtime_service = _adapter()
    asyncio.run(adapter.preflight(_preflight()))

    first = asyncio.run(adapter.handle_inbound(_message()))
    replay = asyncio.run(adapter.handle_inbound(_message()))

    assert first.status == "awaiting_delivery"
    assert first.duplicate is False
    assert replay.status == "awaiting_delivery"
    assert replay.duplicate is True
    assert replay.delivery_idempotency_key == first.delivery_idempotency_key
    assert replay.outbound_text == first.outbound_text
    assert len(runtime_service.calls) == 1
    assert len(repository.quota_calls) == 1
    persisted = _stable_persisted(repository)
    for raw in ("line-raw", "contact-raw", "provider-message-raw", "connection-raw"):
        assert raw not in persisted

    ack = PhotonDeliveryAck(
        receipt_id=first.receipt_id,
        delivery_idempotency_key=first.delivery_idempotency_key,
        connection_id="connection-raw",
        outbound_provider_message_id="outbound-provider-raw",
        acknowledged_at=NOW,
    )
    delivered = asyncio.run(adapter.acknowledge_delivery(ack))
    duplicate_ack = asyncio.run(adapter.acknowledge_delivery(ack))
    delivered_replay = asyncio.run(adapter.handle_inbound(_message()))

    assert delivered.status == "delivered"
    assert duplicate_ack.duplicate is True
    assert delivered_replay.status == "delivered"
    assert delivered_replay.outbound_text is None
    assert len(runtime_service.calls) == 1


def _stable_persisted(repository):
    return json.dumps(
        {
            "binding": repository.binding,
            "messages": repository.messages,
            "claims": repository.claim_calls,
            "acks": repository.ack_calls,
        },
        sort_keys=True,
        default=str,
    )


def test_same_provider_message_with_changed_payload_fails_without_second_turn():
    adapter, _, runtime_service = _adapter()
    asyncio.run(adapter.preflight(_preflight()))
    asyncio.run(adapter.handle_inbound(_message()))

    with pytest.raises(ProvisioningError) as error:
        asyncio.run(adapter.handle_inbound(_message(text="Changed payload.")))

    assert error.value.code == "photon_duplicate_payload_conflict"
    assert len(runtime_service.calls) == 1


@pytest.mark.parametrize(
    ("updates", "expected_code"),
    [
        ({"attachment_count": 1}, "photon_message_scope_forbidden"),
        ({"group_message": True}, "photon_message_scope_forbidden"),
        ({"text": "/model other"}, "photon_command_forbidden"),
    ],
)
def test_forbidden_message_shapes_never_reach_runtime(updates, expected_code):
    adapter, _, runtime_service = _adapter()
    asyncio.run(adapter.preflight(_preflight()))

    with pytest.raises(ProvisioningError) as error:
        asyncio.run(adapter.handle_inbound(_message(**updates)))

    assert error.value.code == expected_code
    assert runtime_service.calls == []


def test_unknown_contact_is_ignored_by_router_contract_and_never_reaches_runtime():
    adapter, _, runtime_service = _adapter()

    with pytest.raises(ProvisioningError) as error:
        asyncio.run(adapter.handle_inbound(_message(contact_identity="not-allowlisted")))

    assert error.value.code == "photon_sender_not_allowed"
    assert runtime_service.calls == []


def test_stale_connection_and_expired_oauth_fail_closed():
    adapter, repository, runtime_service = _adapter()
    asyncio.run(adapter.preflight(_preflight()))
    repository.binding["sidecar_connected_at"] = NOW - timedelta(minutes=2)

    with pytest.raises(ProvisioningError) as stale:
        asyncio.run(adapter.handle_inbound(_message()))
    assert stale.value.code == "photon_sidecar_not_ready"

    repository.binding["sidecar_connected_at"] = datetime.now(timezone.utc)
    repository.binding["oauth_expires_at"] = NOW - timedelta(seconds=1)
    with pytest.raises(ProvisioningError) as expired:
        asyncio.run(adapter.handle_inbound(_message()))
    assert expired.value.code == "photon_sidecar_not_ready"
    assert runtime_service.calls == []


@pytest.mark.parametrize("caller_synthetic", [False, True])
def test_synthetic_only_mode_uses_server_fixture_allowlist_not_caller_flag(
    monkeypatch,
    caller_synthetic,
):
    monkeypatch.setenv("ELLA_HERMES_CLOUD_SYNTHETIC_ONLY", "true")
    adapter, repository, runtime_service = _adapter(
        authorize_synthetic_fixture=False,
    )
    asyncio.run(adapter.preflight(_preflight()))

    with pytest.raises(ProvisioningError) as blocked:
        asyncio.run(
            adapter.handle_inbound(
                _message(synthetic=caller_synthetic),
            )
        )

    assert blocked.value.code == "photon_real_data_not_authorized"
    assert repository.claim_calls == []
    assert repository.quota_calls == []
    assert runtime_service.calls == []


def test_synthetic_fixture_is_server_authorized_when_caller_omits_flag(
    monkeypatch,
):
    monkeypatch.setenv("ELLA_HERMES_CLOUD_SYNTHETIC_ONLY", "true")
    adapter, repository, runtime_service = _adapter()
    asyncio.run(adapter.preflight(_preflight()))

    result = asyncio.run(adapter.handle_inbound(_message(synthetic=False)))

    assert result.status == "awaiting_delivery"
    assert repository.claim_calls
    assert runtime_service.calls[0][1].client_metadata["synthetic"] is True


def test_tool_drift_and_allow_all_fail_preflight():
    drift_adapter, _, runtime_service = _adapter(cloud=FakeCloudClient(tools=("honcho_context", "memory")))
    with pytest.raises(ProvisioningError) as drift:
        asyncio.run(drift_adapter.preflight(_preflight()))
    assert drift.value.code == "photon_runtime_preflight_drift"

    adapter, _, _ = _adapter()
    allow_all = PhotonSidecarPreflight(**{**_preflight().__dict__, "allow_all": True})
    with pytest.raises(ProvisioningError) as broad:
        asyncio.run(adapter.preflight(allow_all))
    assert broad.value.code == "photon_sidecar_policy_drift"
    assert runtime_service.calls == []


def test_runtime_or_writeback_failure_is_uncertain_and_never_returns_delivery():
    adapter, repository, runtime_service = _adapter(
        runtime_failure=ProvisioningError("canonical_writeback_failed", retryable=True)
    )
    asyncio.run(adapter.preflight(_preflight()))

    with pytest.raises(ProvisioningError) as error:
        asyncio.run(adapter.handle_inbound(_message()))

    assert error.value.code == "canonical_writeback_failed"
    assert len(runtime_service.calls) == 1
    assert repository.failures[-1]["uncertain"] is True
    assert next(iter(repository.messages.values()))["status"] == "uncertain"


def test_sidecar_disconnect_after_writeback_returns_no_delivery():
    adapter, repository, runtime_service = _adapter()
    asyncio.run(adapter.preflight(_preflight()))
    original_run_turn = runtime_service.run_turn

    async def disconnect_after_writeback(runtime, request):
        result = await original_run_turn(runtime, request)
        repository.binding["sidecar_connected_at"] = NOW - timedelta(minutes=2)
        return result

    runtime_service.run_turn = disconnect_after_writeback

    with pytest.raises(ProvisioningError) as error:
        asyncio.run(adapter.handle_inbound(_message()))

    assert error.value.code == "photon_sidecar_not_ready"
    receipt = next(iter(repository.messages.values()))
    assert receipt["status"] == "awaiting_delivery"
    assert receipt["canonical_outbound_event_id"] == "canonical-outbound"


def test_stale_claimed_receipt_is_reclaimed_before_quota_or_provider_work():
    adapter, repository, runtime_service = _adapter()
    asyncio.run(adapter.preflight(_preflight()))
    repository.crash_before_quota = True

    with pytest.raises(SimulatedProcessCrash):
        asyncio.run(adapter.handle_inbound(_message()))

    receipt = next(iter(repository.messages.values()))
    assert receipt["status"] == "claimed"
    assert receipt["quota_reserved"] is False
    assert runtime_service.provider_calls == []

    repository.expire_active_lease()
    recovered = asyncio.run(adapter.handle_inbound(_message()))

    assert recovered.status == "awaiting_delivery"
    assert receipt["attempt_count"] == 2
    assert receipt["reconciliation_status"] == "recovered"
    assert len(runtime_service.provider_calls) == 1


def test_stale_running_receipt_is_reclaimed_before_provider_start():
    adapter, repository, runtime_service = _adapter()
    asyncio.run(adapter.preflight(_preflight()))
    repository.crash_before_provider_mark = True

    with pytest.raises(SimulatedProcessCrash):
        asyncio.run(adapter.handle_inbound(_message()))

    receipt = next(iter(repository.messages.values()))
    assert receipt["status"] == "running"
    assert receipt["quota_reserved"] is True
    assert receipt["provider_started"] is False

    repository.expire_active_lease()
    recovered = asyncio.run(adapter.handle_inbound(_message()))

    assert recovered.status == "awaiting_delivery"
    assert len(repository.quota_calls) == 2
    assert len(runtime_service.provider_calls) == 1


def test_completed_runtime_interaction_recovers_without_second_provider_call():
    adapter, repository, runtime_service = _adapter()
    asyncio.run(adapter.preflight(_preflight()))
    repository.crash_on_complete = True

    with pytest.raises(SimulatedProcessCrash):
        asyncio.run(adapter.handle_inbound(_message()))

    receipt = next(iter(repository.messages.values()))
    assert receipt["status"] == "running"
    assert receipt["provider_started"] is True
    assert len(runtime_service.provider_calls) == 1

    repository.expire_active_lease()
    recovered = asyncio.run(adapter.handle_inbound(_message()))

    assert recovered.status == "awaiting_delivery"
    assert len(runtime_service.calls) == 2
    assert len(runtime_service.provider_calls) == 1
    assert receipt["reconciliation_status"] == "recovered"


def test_unconfirmed_provider_started_receipt_requires_manual_reconciliation():
    adapter, repository, runtime_service = _adapter()
    asyncio.run(adapter.preflight(_preflight()))
    runtime_service.crash_after_provider_start = True

    with pytest.raises(SimulatedProcessCrash):
        asyncio.run(adapter.handle_inbound(_message()))

    receipt = next(iter(repository.messages.values()))
    assert receipt["status"] == "running"
    assert receipt["provider_started"] is True
    repository.expire_active_lease()

    with pytest.raises(ProvisioningError) as blocked:
        asyncio.run(adapter.handle_inbound(_message()))

    assert blocked.value.code == "photon_message_uncertain"
    assert blocked.value.retryable is False
    assert receipt["status"] == "uncertain"
    assert receipt["reconciliation_status"] == "manual_required"
    assert len(runtime_service.provider_calls) == 1


def test_conflicting_outbound_provider_ack_fails_closed():
    adapter, _, _ = _adapter()
    asyncio.run(adapter.preflight(_preflight()))
    first = asyncio.run(adapter.handle_inbound(_message()))
    first_ack = PhotonDeliveryAck(
        receipt_id=first.receipt_id,
        delivery_idempotency_key=first.delivery_idempotency_key,
        connection_id="connection-raw",
        outbound_provider_message_id="outbound-provider-a",
        acknowledged_at=NOW,
    )
    asyncio.run(adapter.acknowledge_delivery(first_ack))

    conflicting_ack = PhotonDeliveryAck(
        **{
            **first_ack.__dict__,
            "outbound_provider_message_id": "outbound-provider-b",
        }
    )
    with pytest.raises(ProvisioningError) as error:
        asyncio.run(adapter.acknowledge_delivery(conflicting_ack))

    assert error.value.code == "photon_delivery_ack_conflict"


def test_internal_router_requires_service_token_and_silently_ignores_unknown_sender(
    monkeypatch,
):
    adapter, _, runtime_service = _adapter()

    async def factory():
        return adapter

    app = FastAPI()
    app.include_router(create_photon_router(factory))
    client = TestClient(app)
    payload = {
        "line_identity": "line-raw",
        "contact_identity": "unknown-contact",
        "connection_id": "connection-raw",
        "provider_message_id": "provider-message-raw",
        "text": "Synthetic hello.",
        "occurred_at": NOW.isoformat(),
        "conversation_initiation": False,
        "attachment_count": 0,
        "group_message": False,
        "synthetic": True,
    }
    monkeypatch.setenv("ELLA_HERMES_CLOUD_PHOTON_SIDECAR_TOKEN", "t" * 32)

    unauthorized = client.post(
        "/v1/ella/internal/hermes-cloud/photon/inbound",
        json=payload,
    )
    ignored = client.post(
        "/v1/ella/internal/hermes-cloud/photon/inbound",
        json=payload,
        headers={"X-Ella-Photon-Sidecar-Token": "t" * 32},
    )

    assert unauthorized.status_code == 401
    assert ignored.status_code == 200
    assert ignored.json() == {
        "ok": True,
        "status": "ignored",
        "duplicate": False,
    }
    assert runtime_service.calls == []
