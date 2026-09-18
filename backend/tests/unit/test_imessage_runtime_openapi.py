from pathlib import Path

import yaml

CONTRACT_PATH = Path(__file__).resolve().parents[2] / "ella" / "docs" / "imessage-runtime-internal.openapi.yaml"
FORBIDDEN_SELECTORS = {
    "uid",
    "omi_uid",
    "user_id",
    "account_id",
    "profile_id",
    "runtime_id",
    "runtime_target_id",
    "agent_id",
    "endpoint",
    "credential",
    "provider_route",
}


def _contract():
    return yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))


def _properties(schema):
    properties = set(schema.get("properties", {}))
    for part in schema.get("allOf", []):
        if "$ref" in part:
            referenced = part["$ref"].rsplit("/", 1)[-1]
            properties.update(_properties(_contract()["components"]["schemas"][referenced]))
        else:
            properties.update(_properties(part))
    return properties


def test_internal_runtime_contract_is_transport_only_and_has_no_owner_selector():
    contract = _contract()

    assert contract["x-ella-contract-id"] == "ella.imessage_runtime_transport.v1"
    assert contract["x-ella-cache-policy"] == "no-store"
    assert contract["x-ella-authority"] == {
        "owner-source": "server-binding",
        "runtime-mode": "hermes-chat",
        "fallback": "none",
        "groups": False,
        "attachments": False,
        "transport-can-select-owner": False,
    }
    assert contract["security"] == [{"imessageTransport": []}]
    assert contract["components"]["securitySchemes"]["imessageTransport"] == {
        "type": "apiKey",
        "in": "header",
        "name": "X-Ella-Imessage-Transport-Token",
    }
    assert set(contract["paths"]) == {
        "/v1/ella/internal/imessage/heartbeat",
        "/v1/ella/internal/imessage/inbound",
        "/v1/ella/internal/imessage/delivery/start",
        "/v1/ella/internal/imessage/delivery/ack",
        "/v1/ella/internal/imessage/delivery/uncertain",
        "/v1/ella/internal/imessage/deregister",
    }

    schemas = contract["components"]["schemas"]
    request_schemas = (
        "TransportIdentity",
        "InboundMessage",
        "DeliveryIdentity",
        "DeliveryAck",
        "DeliveryUncertain",
    )
    assert all(_properties(schemas[name]).isdisjoint(FORBIDDEN_SELECTORS) for name in request_schemas)


def test_internal_runtime_contract_separates_model_claim_from_fenced_delivery():
    contract = _contract()
    schemas = contract["components"]["schemas"]

    assert "text" not in schemas["InboundResult"]["required"]
    assert "text" not in schemas["InboundResult"]["properties"]
    assert "fixed_reply" not in schemas["InboundResult"]["properties"]
    assert "binding_generation" in schemas["DeliveryIdentity"]["required"]
    assert schemas["DeliveryStartResult"]["required"] == [
        "status",
        "receipt_id",
        "delivery_idempotency_key",
        "binding_generation",
        "text",
    ]
    assert schemas["DeliveryStartResult"]["properties"]["status"] == {"const": "sending"}
    assert schemas["DeliveryStartResult"]["properties"]["text"]["maxLength"] == 8000
    assert "unknown_sender" in schemas["InboundResult"]["properties"]["status"]["enum"]
    assert "uncertain" in schemas["ReceiptResult"]["properties"]["status"]["enum"]
