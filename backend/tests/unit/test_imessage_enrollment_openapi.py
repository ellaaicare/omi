from pathlib import Path

import yaml

CONTRACT_PATH = Path(__file__).resolve().parents[2] / "ella" / "docs" / "imessage-enrollment.openapi.yaml"
EXPECTED_STATES = {
    "not_connected",
    "verification_pending",
    "ready",
    "temporarily_unavailable",
    "revoked",
}
FORBIDDEN_AUTHORITY_FIELDS = {
    "uid",
    "omi_uid",
    "profile",
    "profile_id",
    "project_id",
    "runtime_id",
    "runtime_target_id",
    "endpoint",
    "credential",
    "provider_route",
}
FORBIDDEN_PUBLIC_FIELDS = {
    "project_id",
    "project_secret",
    "line_identity",
    "contact_identity",
    "provider_user_id",
    "runtime_endpoint",
    "runtime_credential",
}


def _contract():
    return yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))


def test_imessage_enrollment_contract_has_exact_owner_only_surfaces():
    contract = _contract()

    assert contract["x-ella-contract-id"] == "ella.imessage_enrollment.v1"
    assert contract["x-ella-consent-scope"] == "ella.imessage_text_dm.v1"
    assert contract["x-ella-authority"] == {
        "owner": "firebase_bearer_subject",
        "runtime-mode": "hermes-chat",
        "fallback": "none",
        "transport-auth-can-select-owner": False,
    }
    assert set(contract["paths"]) == {
        "/v1/ella/imessage/enrollment",
        "/v1/ella/imessage/enrollment/start",
        "/v1/ella/imessage/enrollment/revoke",
    }
    assert all(path_item[next(iter(path_item))].get("security") is None for path_item in contract["paths"].values())
    assert contract["security"] == [{"bearerAuth": []}]


def test_start_request_cannot_select_tenant_runtime_or_transport_authority():
    properties = set(contract := _contract()["components"]["schemas"]["EnrollmentStartRequest"]["properties"])

    assert properties == {"handset_e164", "consent_receipt_id", "idempotency_key"}
    assert properties.isdisjoint(FORBIDDEN_AUTHORITY_FIELDS)
    assert contract["handset_e164"]["pattern"] == r"^\+[1-9][0-9]{7,14}$"


def test_status_states_are_complete_redacted_and_text_dm_only():
    schemas = _contract()["components"]["schemas"]
    status_properties = set(schemas["EnrollmentStatus"]["properties"])

    assert set(schemas["EnrollmentState"]["enum"]) == EXPECTED_STATES
    assert status_properties.isdisjoint(FORBIDDEN_PUBLIC_FIELDS)
    assert schemas["EnrollmentStatus"]["additionalProperties"] is False
    assert schemas["EnrollmentFeatures"]["properties"] == {
        "text_dm": {"type": "boolean"},
        "groups": {"const": False},
        "attachments": {"const": False},
        "caregiver_delivery": {"const": False},
    }


def test_assigned_destination_is_owner_visible_but_provider_routing_stays_internal():
    contract = _contract()
    serialized = yaml.safe_dump(contract["components"]["schemas"]["EnrollmentStatus"])

    assert "assigned_destination" in serialized
    for field in FORBIDDEN_PUBLIC_FIELDS:
        assert field not in serialized
