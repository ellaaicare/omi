from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from database.account_diagnostics import DiagnosticSupportGrantInvalid
from ella.routers.account_diagnostics import create_account_diagnostics_router, require_diagnostic_operator
from utils.ella.account_diagnostics import (
    DIAGNOSTIC_EVENT_REGISTRY,
    DiagnosticAccountAuthority,
    DiagnosticEventV1,
    DiagnosticFailureCode,
    FAILURE_REGISTRY,
    StoredDiagnosticEvent,
    account_binding_fingerprint,
    generate_support_code,
    project_account_state,
    support_code_hash,
)
from utils.ella.exact_firebase_auth import get_exact_firebase_uid

NOW = datetime(2026, 9, 5, 19, 0, tzinfo=timezone.utc)
FINGERPRINT = "1d30476e4208e7211dad13c42a35bfce8705bf61fe5e5618532ad8b418bfd239"
BACKEND = Path(__file__).resolve().parents[2]


def _event(
    *,
    sequence: int = 0,
    layer: str = "account_binding",
    outcome: str = "succeeded",
    event_name: str = "account_bound",
    failure_code: str | None = None,
    retry_class: str = "never",
    fingerprint: str = FINGERPRINT,
) -> DiagnosticEventV1:
    return DiagnosticEventV1.model_validate(
        {
            "schema_version": "ella.diagnostic_event.v1",
            "event_id": f"event-{sequence}",
            "diagnostic_session_id": "session-1",
            "capture_attempt_id": "attempt-1",
            "account_binding_fingerprint": fingerprint,
            "authority_generation": 4,
            "source_revision": "build-849",
            "layer": layer,
            "event_name": event_name,
            "outcome": outcome,
            "retry_class": retry_class,
            "client_sequence": sequence,
            "client_monotonic_ms": sequence * 10,
            "client_utc_time": (NOW + timedelta(milliseconds=sequence * 10)).isoformat(),
            "stable_failure_code": failure_code,
            "safe_counters": {"retry_number": 0},
        }
    )


def _stored(event: DiagnosticEventV1, seconds: int = 0) -> StoredDiagnosticEvent:
    return StoredDiagnosticEvent(event=event, server_received_at=NOW + timedelta(seconds=seconds))


def test_account_binding_fingerprint_matches_dart_canonical_json_contract():
    assert (
        account_binding_fingerprint(
            uid="uid-a",
            profile_binding_id="aipb_test",
            binding_revision=7,
            consent_receipt_id="aicr_test",
        )
        == FINGERPRINT
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("opaque_resource_id", "device@example.invalid"),
        ("firmware", "https://private.invalid/firmware"),
        ("codec", "opus?uid=secret"),
    ],
)
def test_event_contract_rejects_raw_identifying_values(field, value):
    payload = _event().model_dump(mode="json")
    payload[field] = value
    with pytest.raises(ValidationError, match="raw identifying"):
        DiagnosticEventV1.model_validate(payload)


def test_event_contract_rejects_unknown_fields_and_failure_taxonomy_drift():
    payload = _event().model_dump(mode="json")
    payload["transcript"] = "raw content must never enter diagnostics"
    with pytest.raises(ValidationError, match="extra_forbidden"):
        DiagnosticEventV1.model_validate(payload)

    payload = _event().model_dump(mode="json")
    payload.update(
        outcome="failed",
        stable_failure_code="websocket_auth_failed",
        layer="server_capture",
        event_name="websocket_authenticated",
        retry_class="bounded_automatic",
    )
    with pytest.raises(ValidationError, match="registered layer and retry class"):
        DiagnosticEventV1.model_validate(payload)

    payload = _event().model_dump(mode="json")
    payload["event_name"] = "my_private_note"
    with pytest.raises(ValidationError, match="registered diagnostic layer"):
        DiagnosticEventV1.model_validate(payload)


def test_projection_preserves_missing_evidence_as_unknown():
    projection = project_account_state("session-1", [_stored(_event())], now=NOW)
    assert projection.status == "incomplete"
    assert projection.complete is False
    assert projection.first_unresolved_layer == "ble_transport"
    assert projection.layers[1].state == "unknown"


def test_projection_reports_first_stable_failure_without_claiming_later_layers():
    evidence = [
        _stored(_event(sequence=0), 0),
        _stored(
            _event(
                sequence=1,
                layer="ble_transport",
                outcome="failed",
                event_name="peripheral_connect_failed",
                failure_code="peripheral_connect_timeout",
                retry_class="bounded_automatic",
            ),
            1,
        ),
    ]
    projection = project_account_state("session-1", evidence, now=NOW + timedelta(seconds=2))
    assert projection.status == "failed"
    assert projection.first_unresolved_layer == "ble_transport"
    assert projection.stable_failure_code == "peripheral_connect_timeout"
    assert projection.layers[2].state == "unknown"


def test_support_codes_are_random_format_and_hmac_only():
    code = generate_support_code()
    assert code.startswith("ELLA-") and len(code) == 19
    digest = support_code_hash(code, hmac_key="x" * 32)
    assert len(digest) == 64
    assert code not in digest
    with pytest.raises(ValueError, match="invalid support code"):
        support_code_hash("ELLA-not-valid", hmac_key="x" * 32)


def test_checked_in_schema_registry_and_build_849_golden_traces_match_runtime_contract():
    schema = json.loads((BACKEND / "ella/contracts/diagnostic-event-v1.schema.json").read_text(encoding="utf-8"))
    assert set(schema["$defs"]["eventName"]["enum"]) == set(DIAGNOSTIC_EVENT_REGISTRY)
    registry = json.loads((BACKEND / "ella/contracts/diagnostic-failure-registry-v1.json").read_text(encoding="utf-8"))
    checked_in = {entry["code"]: (entry["layer"], entry["retry_class"]) for entry in registry["entries"]}
    runtime = {code.value: (layer.value, retry.value) for code, (layer, retry) in FAILURE_REGISTRY.items()}
    assert checked_in == runtime
    assert set(checked_in) == {code.value for code in DiagnosticFailureCode}

    expected = {
        "build_849_success.json": ("healthy", None),
        "build_849_ble_timeout.json": ("failed", "peripheral_connect_timeout"),
    }
    for filename, (status_value, failure_code) in expected.items():
        fixture = json.loads((BACKEND / "tests/fixtures/diagnostics" / filename).read_text(encoding="utf-8"))
        events = [DiagnosticEventV1.model_validate(item) for item in fixture["events"]]
        stored = [_stored(event, index) for index, event in enumerate(events)]
        projection = project_account_state(events[0].diagnostic_session_id, stored, now=NOW + timedelta(seconds=10))
        assert projection.status == status_value
        assert projection.stable_failure_code == failure_code


class FakeRepository:
    def __init__(self):
        self.authority = DiagnosticAccountAuthority(
            account_user_id="11111111-1111-1111-1111-111111111111",
            profile_user_id="11111111-1111-1111-1111-111111111111",
            binding_revision=7,
        )
        self.events: list[StoredDiagnosticEvent] = []
        self.support_hash = ""
        self.support_used = False

    async def resolve_account_authority(self, _uid):
        return self.authority

    async def append_events(self, _authority, events):
        self.events.extend(_stored(event, index) for index, event in enumerate(events))
        return len(events), 0

    async def list_session_events(self, _authority, diagnostic_session_id):
        return [item for item in self.events if item.event.diagnostic_session_id == diagnostic_session_id]

    async def create_support_grant(self, _authority, **kwargs):
        self.support_hash = kwargs["code_hash"]
        return "22222222-2222-2222-2222-222222222222"

    async def revoke_support_grant(self, _authority, _grant_id):
        return True

    async def consume_support_grant(self, *, code_hash, **_kwargs):
        if self.support_used or code_hash != self.support_hash:
            raise DiagnosticSupportGrantInvalid
        self.support_used = True
        return "session-1", list(self.events)


def _client(monkeypatch):
    monkeypatch.setenv("ELLA_DIAGNOSTICS_SUPPORT_HMAC_KEY", "support-hmac-key-that-is-at-least-32-bytes")
    repository = FakeRepository()
    app = FastAPI()
    app.include_router(
        create_account_diagnostics_router(
            repository,
            consent_status=lambda _uid: {
                "authorized": True,
                "consent": {"profile_binding_id": "aipb_test", "receipt_id": "aicr_test"},
            },
            clock=lambda: NOW,
        )
    )
    app.dependency_overrides[get_exact_firebase_uid] = lambda: "uid-a"
    app.dependency_overrides[require_diagnostic_operator] = lambda: "support@example.invalid"
    return TestClient(app), repository


def test_ingest_rejects_cross_account_fingerprint_before_write(monkeypatch):
    client, repository = _client(monkeypatch)
    payload = {"events": [_event(fingerprint="0" * 64).model_dump(mode="json")]}
    response = client.post("/v1/ella/diagnostics/events", json=payload)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "diagnostic_account_binding_stale"
    assert repository.events == []


def test_account_ingest_projection_and_single_use_audited_support_exchange(monkeypatch):
    client, repository = _client(monkeypatch)
    event = _event()
    ingest = client.post("/v1/ella/diagnostics/events", json={"events": [event.model_dump(mode="json")]})
    assert ingest.status_code == 202
    assert ingest.json() == {
        "schema_version": "ella.diagnostic_ingest_receipt.v1",
        "accepted": 1,
        "duplicates": 0,
        "evidence_only": True,
    }
    projection = client.get("/v1/ella/diagnostics/projection/session-1")
    assert projection.status_code == 200
    assert projection.json()["status"] == "incomplete"
    assert "uid-a" not in projection.text

    grant = client.post("/v1/ella/diagnostics/support-grants", json={"diagnostic_session_id": "session-1"})
    assert grant.status_code == 201
    code = grant.json()["support_code"]
    exchange_payload = {"support_code": code, "case_id": "case-1258", "reason": "customer_requested_help"}
    exchange = client.post("/v1/ella/operator/diagnostics/support-code/exchange", json=exchange_payload)
    assert exchange.status_code == 200
    assert exchange.json()["operator_id"] == "support@example.invalid"
    assert exchange.json()["projection"]["diagnostic_session_id"] == "session-1"
    assert "uid-a" not in exchange.text

    repeated = client.post("/v1/ella/operator/diagnostics/support-code/exchange", json=exchange_payload)
    assert repeated.status_code == 404
    assert repeated.json()["detail"]["code"] == "diagnostic_support_code_invalid"
    assert repository.support_used is True
