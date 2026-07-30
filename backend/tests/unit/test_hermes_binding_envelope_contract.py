"""Cross-repository HermesBindingEnvelope v1 contract checks."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import jsonschema

from ella.services.hermes_broker_client import HermesBrokerClient
from ella.services.hermes_broker_prototype import HermesBrokerPrototypeConfig
from ella.services.hermes_cloud_runtime import broker_session_id_for_scope

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
ENVELOPE_PATH = FIXTURES / "hermes_binding_envelope_v1.json"
SCHEMA_PATH = FIXTURES / "hermes_binding_envelope_v1.schema.json"
ENVELOPE_SHA256 = "e4140b26a9ea0b9e7110fd092884913fa10556d63e689eb44c95e55171f577b3"
SCHEMA_SHA256 = "b57c94b6a2a116f166b7662afa83fc5b8b75f984c7c29802ec1b45f35d0ecfeb"


class _Response:
    status_code = 200

    def __init__(self, body):
        self._body = body
        self.content = json.dumps(body, separators=(",", ":")).encode()

    def json(self):
        return self._body


class _CaptureHttp:
    def __init__(self):
        self.call = None

    def factory(self, timeout):
        del timeout
        parent = self

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def post(self, url, **kwargs):
                parent.call = ("POST", url, kwargs)
                return _Response(
                    {
                        "status": "pending",
                        "request_id": "hwb_contract_request",
                        "correlation_id": "hwb:contract-correlation",
                        "delivery_platform": "ella_callback_stock",
                        "callback_source": "hermes_stock_0_19_quiet_window",
                        "callback_contract": "stock_best_effort_v1",
                        "terminal_proof": False,
                    }
                )

        return _Client()


def _load_contract():
    return json.loads(ENVELOPE_PATH.read_text(encoding="utf-8"))


def test_hermes_binding_envelope_v1_identity_and_content_free_shape():
    assert hashlib.sha256(ENVELOPE_PATH.read_bytes()).hexdigest() == ENVELOPE_SHA256
    assert hashlib.sha256(SCHEMA_PATH.read_bytes()).hexdigest() == SCHEMA_SHA256
    envelope = _load_contract()
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(
        schema,
        format_checker=jsonschema.FormatChecker(),
    ).validate(envelope)
    assert schema["title"] == "HermesBindingEnvelope v1"
    assert envelope["schema_version"] == "ella-hermes-binding-envelope-v1"
    assert envelope["account_id"] == envelope["profile_id"]
    uid = envelope["uid"]
    expected = hashlib.sha256(f"ella-managed-cloud-profile-v1\x1f{uid}\x1f{uid}".encode()).hexdigest()
    assert envelope["binding"]["profile_binding_id"] == f"aipb_{expected[:32]}"
    assert envelope["binding"]["profile_class"] == "synthetic"
    assert envelope["authority"]["content_free_receipt"] == envelope["binding"]["prompt_artifact_receipt"]
    assert envelope["authority"]["content_free_receipt"]["content_free"] is True
    assert envelope["request"]["consent_epoch"] == envelope["authority"]["consent_epoch"]
    assert envelope["request"]["canonical_user_event_id"] == envelope["request"]["source_event_id"]
    assert envelope["request"]["callback_contract"] == "stock_best_effort_v1"
    assert envelope["request"]["terminal_proof"] is False
    assert envelope["result_contract"] == {
        "success_status": "writeback_completed",
        "callback_contract": "stock_best_effort_v1",
        "terminal_proof": False,
        "diagnostic_fields": ["stage", "reason", "generation"],
    }
    broker_session = envelope["broker_session"]
    assert broker_session["session_id"] == broker_session_id_for_scope(
        account_id=envelope["account_id"],
        profile_id=envelope["profile_id"],
        runtime_binding_ref=envelope["binding"]["id"],
        channel=broker_session["channel"],
        session_key=broker_session["session_key"],
    )
    assert envelope["secret_references"] == {
        "broker_service_token": "env:ELLA_HERMES_BROKER_SERVICE_TOKEN",
        "broker_ingress_hmac": "env:ELLA_HERMES_WEBHOOK_INGRESS_HMAC_SECRET",
        "stock_callback_hmac": "env:ELLA_HERMES_STOCK_CALLBACK_HMAC_SECRET",
    }


def test_omi_request_uses_exact_stock_contract_from_shared_envelope(monkeypatch):
    envelope = _load_contract()
    monkeypatch.setenv(
        "ELLA_HERMES_BROKER_SERVICE_TOKEN",
        "synthetic-test-token",
    )
    capture = _CaptureHttp()
    config = HermesBrokerPrototypeConfig(
        enabled=True,
        account_id=envelope["account_id"],
        profile_id=envelope["profile_id"],
        binding_id=envelope["binding"]["id"],
        base_url="https://broker.ella.test",
        allowed_host="broker.ella.test",
        service_token_ref=envelope["secret_references"]["broker_service_token"],
        poll_interval_seconds=0.1,
        poll_timeout_seconds=1.0,
        deadline_seconds=30,
    )
    client = HermesBrokerClient(config, http_client_factory=capture.factory)
    request = envelope["request"]
    asyncio.run(
        client.admit(
            account_id=envelope["account_id"],
            profile_id=envelope["profile_id"],
            runtime_binding_ref=envelope["binding"]["id"],
            lane=request["lane"],
            source_event_id=request["source_event_id"],
            consent_epoch=request["consent_epoch"],
            payload={
                "message": "synthetic-contract-payload",
                "session_key": envelope["broker_session"]["session_key"],
                "session_id": envelope["broker_session"]["session_id"],
                "canonical_user_event_id": request["canonical_user_event_id"],
            },
            deadline_at=1_900_000_000,
        )
    )

    method, url, kwargs = capture.call
    assert method == "POST"
    assert url.endswith("/stock-canary/admit")
    assert "/hermes-webhook-broker/admit" not in url
    assert kwargs["json"]["delivery_platform"] == request["delivery_platform"]
    assert kwargs["json"]["callback_source"] == request["callback_source"]
    assert kwargs["json"]["webhook_route"] == request["webhook_route"]
    assert kwargs["json"]["runtime_binding_ref"] == envelope["binding"]["id"]
    assert kwargs["json"]["consent_epoch"] == envelope["authority"]["consent_epoch"]
    assert kwargs["json"]["source_event_id"] == kwargs["json"]["payload"]["canonical_user_event_id"]
    assert kwargs["headers"]["Authorization"].startswith("Bearer ")
