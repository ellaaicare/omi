import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ella.routers.canonical_events import InMemoryCanonicalEventStore
from ella.routers.imessage_runtime import create_imessage_runtime_router
from ella.services.imessage_runtime import (
    ImessageDeliveryIdentity,
    ImessageInbound,
    ImessageRuntimeError,
    ImessageRuntimeService,
)
from ella.services.runtime_errors import ProvisioningError
from ella.services.runtime_resolver import IsolatedRuntime, runtime_authority_identity

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
BINDING_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
TARGET_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
USER_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")
CONSENT_ID = uuid.UUID("44444444-4444-4444-8444-444444444444")
RECEIPT_ID = uuid.UUID("55555555-5555-4555-8555-555555555555")
DELIVERY_KEY = uuid.UUID("66666666-6666-4666-8666-666666666666")
LEASE_TOKEN = uuid.UUID("77777777-7777-4777-8777-777777777777")
TRANSPORT_TOKEN = "t" * 32


def _runtime(**overrides) -> IsolatedRuntime:
    values = {
        "uid": "owner-a",
        "binding_id": str(BINDING_ID),
        "provider": "hermes",
        "status": "active",
        "profile_name": "omi-owner-a",
        "agent_id": "ella-main-agent",
        "runtime_instance_id": "instance-owner-a",
        "gateway_url": "http://127.0.0.1:8642",
        "gateway_token": "runtime-test-token",
        "workspace_root": "/srv/ella/owner-a",
        "honcho_workspace": "honcho-owner-a",
        "observed_peer": "owner-a",
        "observer_peer": "ella-owner-a",
        "prompt_pack_version": "hermes-user-v1",
        "expected_model": "ella-main-agent",
        "model_context_window_tokens": 128000,
        "allowed_tools": (),
        "required_capabilities": (),
        "model_policy_version": "frontier-v1",
        "voice_policy_version": "ella-voice-v1",
        "revision": 7,
        "profile_class": "real",
        "runtime_target_id": str(TARGET_ID),
        "runtime_target_mode": "hermes-chat",
        "runtime_target_updated_at": NOW.isoformat(),
        "target_endpoint_ref": "",
        "target_credential_ref": "",
        "target_entitlement_revision": 11,
        "consent_authority_epoch": "88888888-8888-4888-8888-888888888888",
        "account_user_id": str(USER_ID),
        "profile_user_id": str(USER_ID),
    }
    values.update(overrides)
    return IsolatedRuntime(**values)


def _binding(runtime: IsolatedRuntime) -> dict:
    return {
        "id": uuid.UUID("99999999-9999-4999-8999-999999999999"),
        "user_id": USER_ID,
        "omi_uid": runtime.uid,
        "status": "active",
        "generation": 3,
        "revision": 4,
        "line_identity_hmac": "a" * 64,
        "contact_identity_hmac": "b" * 64,
        "transport_connection_ref_hmac": "",
        "last_transport_healthy_at": None,
        "consent_receipt_id": CONSENT_ID,
        "consent_authority_epoch": uuid.UUID(runtime.consent_authority_epoch),
        "runtime_binding_id": BINDING_ID,
        "runtime_target_id": TARGET_ID,
        "runtime_authority_digest": runtime_authority_identity(runtime).digest,
    }


class FakeRepository:
    def __init__(self, binding: dict | None):
        self.binding = binding
        self.events: list[tuple[str, dict]] = []
        self.receipt = {
            "id": RECEIPT_ID,
            "status": "claimed",
            "delivery_idempotency_key": DELIVERY_KEY,
            "binding_generation": 3,
            "lease_token": LEASE_TOKEN,
            "model_started": False,
            "outbound_text": None,
        }

    async def assert_schema_ready(self):
        self.events.append(("schema", {}))

    async def resolve_binding(self, **kwargs):
        self.events.append(("resolve", kwargs))
        return dict(self.binding) if self.binding else None

    async def record_heartbeat(self, **kwargs):
        self.events.append(("heartbeat", kwargs))
        self.binding["transport_connection_ref_hmac"] = kwargs["connection_ref_hmac"]
        self.binding["last_transport_healthy_at"] = NOW
        return dict(self.binding)

    async def claim_message(self, **kwargs):
        self.events.append(("claim", kwargs))
        return {**self.receipt, "acquired": True, "duplicate": False, "reclaimed": False}

    async def mark_model_started(self, **kwargs):
        self.events.append(("model_started", kwargs))
        self.receipt.update(status="running", model_started=True)
        return dict(self.receipt)

    async def complete_model(self, **kwargs):
        self.events.append(("model_complete", kwargs))
        self.receipt.update(
            status="awaiting_delivery",
            outbound_text=kwargs["outbound_text"],
            canonical_inbound_event_id=kwargs["canonical_inbound_event_id"],
            canonical_outbound_event_id=kwargs["canonical_outbound_event_id"],
        )
        return dict(self.receipt)

    async def fail_message(self, **kwargs):
        self.events.append(("failed", kwargs))
        self.receipt["status"] = "uncertain" if kwargs["uncertain"] else "failed"

    async def start_delivery(self, **kwargs):
        self.events.append(("delivery_started", kwargs))
        self.receipt["status"] = "sending"
        return dict(self.receipt)

    async def acknowledge_delivery(self, **kwargs):
        self.events.append(("delivery_ack", kwargs))
        self.receipt["status"] = "delivered"
        return dict(self.receipt)

    async def mark_delivery_uncertain(self, **kwargs):
        self.events.append(("delivery_uncertain", kwargs))
        self.receipt["status"] = "uncertain"
        return dict(self.receipt)

    async def reconcile_pre_send_delivery(self, **kwargs):
        self.events.append(("delivery_reconcile", kwargs))
        self.receipt["status"] = "quarantined"
        return dict(self.receipt)

    async def quarantine_binding(self, **kwargs):
        self.events.append(("deregister", kwargs))
        self.binding["status"] = "quarantined"
        return dict(self.binding)


class FakeCompletionClient:
    def __init__(self, events, *, error=None, response="A concise reply"):
        self.events = events
        self.error = error
        self.response = response
        self.calls = 0

    async def complete(self, **kwargs):
        self.calls += 1
        self.events.append(("model", kwargs))
        if self.error:
            raise self.error
        return self.response


def _service(repository, completion_client, runtime=None, *, revalidator=None):
    runtime = runtime or _runtime()

    async def resolve(*_args, **_kwargs):
        return runtime

    async def revalidate(_identity):
        if revalidator is not None:
            return await revalidator(_identity)
        return runtime

    return ImessageRuntimeService(
        repository=repository,
        event_store=InMemoryCanonicalEventStore(),
        completion_client=completion_client,
        hmac_key=b"h" * 32,
        now=lambda: NOW,
        runtime_resolver=resolve,
        runtime_revalidator=revalidate,
    )


def _inbound(**overrides) -> ImessageInbound:
    values = {
        "line_identity": "line-a",
        "contact_identity": "contact-a",
        "connection_id": "connection-a",
        "provider_message_id": "provider-message-a",
        "text": "Hello Ella",
        "occurred_at": NOW,
    }
    values.update(overrides)
    return ImessageInbound(**values)


def _delivery() -> ImessageDeliveryIdentity:
    return ImessageDeliveryIdentity(
        line_identity="line-a",
        contact_identity="contact-a",
        connection_id="connection-a",
        receipt_id=str(RECEIPT_ID),
        delivery_idempotency_key=str(DELIVERY_KEY),
        binding_generation=3,
    )


def test_feature_flag_defaults_off_before_repository_or_model(monkeypatch):
    monkeypatch.delenv("ELLA_IMESSAGE_RUNTIME_ENABLED", raising=False)
    runtime = _runtime()
    repository = FakeRepository(_binding(runtime))
    client = FakeCompletionClient(repository.events)

    with pytest.raises(ImessageRuntimeError, match="imessage_runtime_disabled"):
        asyncio.run(_service(repository, client, runtime).ingest(_inbound()))

    assert repository.events == []
    assert client.calls == 0


def test_unknown_sender_returns_status_only_without_model_write_or_replayable_reply(monkeypatch):
    monkeypatch.setenv("ELLA_IMESSAGE_RUNTIME_ENABLED", "true")
    repository = FakeRepository(None)
    client = FakeCompletionClient(repository.events)

    result = asyncio.run(_service(repository, client).ingest(_inbound()))

    assert result == {
        "status": "unknown_sender",
        "model_invoked": False,
    }
    assert [event[0] for event in repository.events] == ["schema", "resolve"]
    assert client.calls == 0


def test_text_dm_claim_precedes_model_and_delivery_requires_send_start(monkeypatch):
    monkeypatch.setenv("ELLA_IMESSAGE_RUNTIME_ENABLED", "true")
    runtime = _runtime()
    repository = FakeRepository(_binding(runtime))
    client = FakeCompletionClient(repository.events)
    service = _service(repository, client, runtime)

    heartbeat = asyncio.run(
        service.heartbeat(line_identity="line-a", contact_identity="contact-a", connection_id="connection-a")
    )
    result = asyncio.run(service.ingest(_inbound()))

    assert heartbeat["status"] == "ready"
    assert result["status"] == "awaiting_delivery"
    assert "text" not in result
    names = [event[0] for event in repository.events]
    assert names.index("claim") < names.index("model_started") < names.index("model") < names.index("model_complete")
    events = asyncio.run(service.event_store.timeline(uid="owner-a", since=None, limit=10, channels=["imessage"]))
    assert [(event["role"], event["text"]) for event in events] == [
        ("user", "Hello Ella"),
        ("assistant", "A concise reply"),
    ]

    delivery = asyncio.run(service.start_delivery(_delivery()))
    assert delivery == {
        "status": "sending",
        "receipt_id": str(RECEIPT_ID),
        "delivery_idempotency_key": str(DELIVERY_KEY),
        "binding_generation": 3,
        "text": "A concise reply",
    }
    acknowledged = asyncio.run(
        service.acknowledge_delivery(_delivery(), outbound_provider_message_id="outbound-message-a")
    )
    assert acknowledged == {"status": "delivered", "receipt_id": str(RECEIPT_ID)}


def test_provider_length_reply_is_rejected_before_canonical_write_or_delivery(monkeypatch):
    monkeypatch.setenv("ELLA_IMESSAGE_RUNTIME_ENABLED", "true")
    runtime = _runtime()
    repository = FakeRepository(_binding(runtime))
    client = FakeCompletionClient(repository.events, response="x" * 8_001)
    service = _service(repository, client, runtime)
    asyncio.run(service.heartbeat(line_identity="line-a", contact_identity="contact-a", connection_id="connection-a"))

    with pytest.raises(ImessageRuntimeError, match="imessage_model_response_invalid"):
        asyncio.run(service.ingest(_inbound()))

    events = asyncio.run(service.event_store.timeline(uid="owner-a", since=None, limit=10, channels=["imessage"]))
    assert [(event["role"], event["text"]) for event in events] == [("user", "Hello Ella")]
    assert repository.receipt["status"] == "uncertain"
    assert not any(name == "model_complete" for name, _payload in repository.events)


def test_group_and_attachment_are_rejected_before_claim_or_model(monkeypatch):
    monkeypatch.setenv("ELLA_IMESSAGE_RUNTIME_ENABLED", "true")
    runtime = _runtime()
    repository = FakeRepository(_binding(runtime))
    client = FakeCompletionClient(repository.events)
    service = _service(repository, client, runtime)

    cases = (
        (_inbound(group_message=True), "imessage_message_scope_forbidden"),
        (_inbound(attachment_count=1), "imessage_message_scope_forbidden"),
        (_inbound(occurred_at=NOW - timedelta(minutes=11)), "imessage_message_timestamp_invalid"),
        (_inbound(occurred_at=NOW + timedelta(minutes=3)), "imessage_message_timestamp_invalid"),
        (_inbound(occurred_at=NOW.replace(tzinfo=None)), "imessage_message_timestamp_invalid"),
    )
    for request, code in cases:
        with pytest.raises(ImessageRuntimeError, match=code):
            asyncio.run(service.ingest(request))

    assert [event[0] for event in repository.events] == ["schema"] * len(cases)
    assert client.calls == 0


def test_authority_drift_before_model_has_zero_provider_effect(monkeypatch):
    monkeypatch.setenv("ELLA_IMESSAGE_RUNTIME_ENABLED", "true")
    runtime = _runtime()
    repository = FakeRepository(_binding(runtime))
    client = FakeCompletionClient(repository.events)

    async def drift(_identity):
        raise ProvisioningError("hermes_runtime_authority_changed", retryable=False)

    service = _service(repository, client, runtime, revalidator=drift)
    asyncio.run(service.heartbeat(line_identity="line-a", contact_identity="contact-a", connection_id="connection-a"))

    with pytest.raises(ImessageRuntimeError, match="hermes_runtime_authority_changed"):
        asyncio.run(service.ingest(_inbound()))

    assert client.calls == 0
    assert repository.events[-1][0] == "failed"
    assert repository.events[-1][1]["uncertain"] is False


def test_model_error_after_send_boundary_is_quarantined_without_retry(monkeypatch):
    monkeypatch.setenv("ELLA_IMESSAGE_RUNTIME_ENABLED", "true")
    runtime = _runtime()
    repository = FakeRepository(_binding(runtime))
    client = FakeCompletionClient(
        repository.events,
        error=ImessageRuntimeError("imessage_model_transport_uncertain", status_code=503),
    )
    service = _service(repository, client, runtime)
    asyncio.run(service.heartbeat(line_identity="line-a", contact_identity="contact-a", connection_id="connection-a"))

    with pytest.raises(ImessageRuntimeError, match="imessage_model_transport_uncertain"):
        asyncio.run(service.ingest(_inbound()))

    assert client.calls == 1
    assert repository.events[-1][0] == "failed"
    assert repository.events[-1][1]["uncertain"] is True

    async def duplicate_claim(**_kwargs):
        return {**repository.receipt, "acquired": False, "duplicate": True, "reclaimed": False}

    repository.claim_message = duplicate_claim
    replay = asyncio.run(service.ingest(_inbound()))
    assert replay["status"] == "uncertain"
    assert client.calls == 1


def test_pre_send_reconcile_is_terminal_without_current_runtime_or_reply_text(monkeypatch):
    monkeypatch.delenv("ELLA_IMESSAGE_RUNTIME_ENABLED", raising=False)
    runtime = _runtime()
    repository = FakeRepository(_binding(runtime))
    client = FakeCompletionClient(repository.events)

    result = asyncio.run(_service(repository, client, runtime).reconcile_pre_send_delivery(_delivery()))

    assert result == {
        "status": "quarantined",
        "receipt_id": str(RECEIPT_ID),
        "retryable": False,
    }
    assert [event[0] for event in repository.events] == ["schema", "delivery_reconcile"]
    assert client.calls == 0
    assert "text" not in result


def test_transport_routes_fail_closed_and_forbid_uid_selectors(monkeypatch):
    calls = []

    class RouteService:
        async def heartbeat(self, **kwargs):
            calls.append(kwargs)
            return {"status": "ready"}

        async def reconcile_pre_send_delivery(self, request):
            calls.append(request)
            return {"status": "quarantined", "receipt_id": request.receipt_id, "retryable": False}

    async def factory():
        return RouteService()

    app = FastAPI()
    app.include_router(create_imessage_runtime_router(factory))
    client = TestClient(app)
    payload = {"line_identity": "line-a", "contact_identity": "contact-a", "connection_id": "connection-a"}

    monkeypatch.delenv("ELLA_IMESSAGE_TRANSPORT_TOKEN", raising=False)
    for headers in ({}, {"X-Ella-Imessage-Transport-Token": ""}, {"X-Ella-Imessage-Transport-Token": " "}):
        response = client.post("/v1/ella/internal/imessage/heartbeat", json=payload, headers=headers)
        assert response.status_code == 503
    assert calls == []

    monkeypatch.setenv("ELLA_IMESSAGE_TRANSPORT_TOKEN", TRANSPORT_TOKEN)
    wrong = client.post(
        "/v1/ella/internal/imessage/heartbeat",
        json=payload,
        headers={"X-Ella-Imessage-Transport-Token": "x" * 32},
    )
    assert wrong.status_code == 401
    selected = client.post(
        "/v1/ella/internal/imessage/heartbeat",
        json={**payload, "uid": "attacker-selected"},
        headers={"X-Ella-Imessage-Transport-Token": TRANSPORT_TOKEN},
    )
    assert selected.status_code == 422
    assert calls == []

    malformed_delivery = client.post(
        "/v1/ella/internal/imessage/delivery/start",
        json={
            **payload,
            "receipt_id": "not-a-uuid",
            "delivery_idempotency_key": str(DELIVERY_KEY),
            "binding_generation": 3,
        },
        headers={"X-Ella-Imessage-Transport-Token": TRANSPORT_TOKEN},
    )
    assert malformed_delivery.status_code == 422
    assert calls == []

    allowed = client.post(
        "/v1/ella/internal/imessage/heartbeat",
        json=payload,
        headers={"X-Ella-Imessage-Transport-Token": TRANSPORT_TOKEN},
    )
    assert allowed.status_code == 200
    assert allowed.headers["cache-control"] == "no-store"
    reconciled = client.post(
        "/v1/ella/internal/imessage/delivery/reconcile",
        json={
            **payload,
            "receipt_id": str(RECEIPT_ID),
            "delivery_idempotency_key": str(DELIVERY_KEY),
            "binding_generation": 3,
        },
        headers={"X-Ella-Imessage-Transport-Token": TRANSPORT_TOKEN},
    )
    assert reconciled.status_code == 200
    assert reconciled.headers["cache-control"] == "no-store"
    assert reconciled.json()["status"] == "quarantined"
    assert calls[0] == payload
    assert isinstance(calls[1], ImessageDeliveryIdentity)


def test_deregister_feature_flag_defaults_off_before_repository_mutation(monkeypatch):
    monkeypatch.delenv("ELLA_IMESSAGE_RUNTIME_ENABLED", raising=False)
    runtime = _runtime()
    repository = FakeRepository(_binding(runtime))
    client = FakeCompletionClient(repository.events)

    with pytest.raises(ImessageRuntimeError, match="imessage_runtime_disabled"):
        asyncio.run(
            _service(repository, client, runtime).deregister(
                line_identity="line-a",
                contact_identity="contact-a",
                connection_id="connection-a",
            )
        )

    assert repository.events == []


def test_terminal_delivery_callbacks_use_started_receipt_without_current_authority(monkeypatch):
    monkeypatch.setenv("ELLA_IMESSAGE_RUNTIME_ENABLED", "true")
    runtime = _runtime()
    repository = FakeRepository(_binding(runtime))
    client = FakeCompletionClient(repository.events)
    service = _service(repository, client, runtime)
    asyncio.run(service.heartbeat(line_identity="line-a", contact_identity="contact-a", connection_id="connection-a"))
    asyncio.run(service.ingest(_inbound()))
    asyncio.run(service.start_delivery(_delivery()))

    repository.binding = None
    before = len(repository.events)
    acknowledged = asyncio.run(
        service.acknowledge_delivery(_delivery(), outbound_provider_message_id="outbound-message-a")
    )

    assert acknowledged == {"status": "delivered", "receipt_id": str(RECEIPT_ID)}
    terminal_events = repository.events[before:]
    assert [name for name, _ in terminal_events] == ["schema", "delivery_ack"]
    assert "binding_id" not in terminal_events[-1][1]
    assert "line_identity_hmac" in terminal_events[-1][1]
    assert "connection_ref_hmac" in terminal_events[-1][1]


def test_terminal_delivery_uncertain_works_with_runtime_flag_off_after_send_start(monkeypatch):
    monkeypatch.setenv("ELLA_IMESSAGE_RUNTIME_ENABLED", "true")
    runtime = _runtime()
    repository = FakeRepository(_binding(runtime))
    service = _service(repository, FakeCompletionClient(repository.events), runtime)
    asyncio.run(service.heartbeat(line_identity="line-a", contact_identity="contact-a", connection_id="connection-a"))
    asyncio.run(service.ingest(_inbound()))
    asyncio.run(service.start_delivery(_delivery()))

    monkeypatch.setenv("ELLA_IMESSAGE_RUNTIME_ENABLED", "false")
    result = asyncio.run(service.mark_delivery_uncertain(_delivery(), error_code="provider_outcome_unconfirmed"))

    assert result == {"status": "uncertain", "receipt_id": str(RECEIPT_ID), "retryable": False}
    assert repository.events[-2][0] == "schema"
    assert repository.events[-1][0] == "delivery_uncertain"


def test_transport_private_successes_and_errors_are_no_store(monkeypatch):
    calls = []

    class RouteService:
        async def heartbeat(self, **kwargs):
            calls.append(kwargs)
            return {"status": "ready"}

        async def start_delivery(self, _identity):
            raise ImessageRuntimeError("imessage_delivery_not_ready", status_code=409)

    async def factory():
        return RouteService()

    monkeypatch.setenv("ELLA_IMESSAGE_TRANSPORT_TOKEN", TRANSPORT_TOKEN)
    app = FastAPI()
    app.include_router(create_imessage_runtime_router(factory))
    client = TestClient(app)
    identity = {
        "line_identity": "line-a",
        "contact_identity": "contact-a",
        "connection_id": "connection-a",
    }

    success = client.post(
        "/v1/ella/internal/imessage/heartbeat",
        json=identity,
        headers={"X-Ella-Imessage-Transport-Token": TRANSPORT_TOKEN},
    )
    error = client.post(
        "/v1/ella/internal/imessage/delivery/start",
        json={
            **identity,
            "receipt_id": str(RECEIPT_ID),
            "delivery_idempotency_key": str(DELIVERY_KEY),
            "binding_generation": 3,
        },
        headers={"X-Ella-Imessage-Transport-Token": TRANSPORT_TOKEN},
    )
    unauthenticated = client.post("/v1/ella/internal/imessage/heartbeat", json=identity)

    assert success.status_code == 200
    assert error.status_code == 409
    assert unauthenticated.status_code == 401
    assert {response.headers.get("cache-control") for response in (success, error, unauthenticated)} == {"no-store"}
