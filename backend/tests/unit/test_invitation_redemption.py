import asyncio
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.requests import Request

conversations_module = ModuleType("database.conversations")
conversations_module._decrypt_conversation_data = lambda value, uid=None: value
sys.modules.setdefault("database.conversations", conversations_module)
sys.modules.setdefault("database.proposals", ModuleType("database.proposals"))
sys.modules.setdefault("websockets", ModuleType("websockets"))

from database import invitations
from ella.routers import invites
from ella.routers import voice
from ella.services import ai_consent
from ella.services import invitation_authority
from utils.other import endpoints as auth


def _request(*, client: str, headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/invite/redeem",
            "headers": headers or [],
            "client": (client, 1234),
            "server": ("api.ella-ai-care.com", 443),
            "scheme": "https",
            "query_string": b"",
        }
    )


def test_code_normalization_and_generation_use_non_ambiguous_alphabet():
    assert invitations.normalize_invite_code("ab cd-2345") == "ABCD2345"
    assert invitations.normalize_invite_code("ABCI-2345") == ""
    assert invitations.normalize_invite_code("ABCD-234") == ""
    for _ in range(500):
        generated = invitations.generate_invite_code()
        assert generated[4] == "-"
        assert invitations.normalize_invite_code(generated)


def test_request_contract_forbids_caller_identity_and_policy():
    with pytest.raises(ValidationError):
        invites.InviteRedeemRequest(code="ABCD-2345", uid="attacker")
    with pytest.raises(ValidationError):
        invites.InviteRedeemRequest(code="ABCD-2345", entitlement={"status": "active"})


def test_feature_flags_default_off(monkeypatch):
    monkeypatch.delenv("ELLA_INVITE_HMAC_PEPPER", raising=False)
    monkeypatch.delenv("ELLA_INVITE_REDEMPTION_ENABLED", raising=False)
    monkeypatch.delenv("ELLA_INVITE_ORDINARY_SELF_SERVICE_ENABLED", raising=False)
    monkeypatch.delenv("ELLA_INVITE_APP_REVIEW_ENABLED", raising=False)

    config = invitations.InvitationConfig.from_env()

    assert not config.redemption_enabled
    assert not config.ordinary_enabled
    assert not config.app_review_enabled


def test_enabled_redemption_requires_strong_server_pepper(monkeypatch):
    monkeypatch.setenv("ELLA_INVITE_REDEMPTION_ENABLED", "true")
    monkeypatch.setenv("ELLA_INVITE_HMAC_PEPPER", "too-short")
    with pytest.raises(invitations.InviteConfigurationError):
        invitations.InvitationConfig.from_env()


def test_disabled_redemption_never_touches_storage():
    config = invitations.InvitationConfig(hmac_pepper=b"unit-test-only")
    with pytest.raises(invitations.InviteRedemptionFailure) as error:
        asyncio.run(
            invitations.redeem_invitation(
                uid="uid-a",
                code="ABCD-2345",
                source_address="192.0.2.10",
                config=config,
            )
        )
    assert error.value.code == "invalid"
    assert error.value.status_code == 400


def test_policy_rejects_enabled_fallback_and_unbounded_values():
    policy = {
        "plan": "canary",
        "daily_limit_s": 2700,
        "monthly_limit_s": 43200,
        "max_session_s": 1200,
        "max_concurrent": 1,
        "max_audio_bytes_per_session": 120_000_000,
        "max_audio_bytes_per_minute": 6_000_000,
        "provider_allowlist": ["grok-voice"],
        "model_allowlist": [],
        "mode_allowlist": ["v4"],
        "fallback_policy": {"enabled": True, "order": ["legacy"]},
    }
    with pytest.raises(invitations.InviteConfigurationError):
        invitations.normalize_entitlement_policy(policy)
    policy["max_concurrent"] = 1
    policy["soft_limit_ratio"] = "nan"
    with pytest.raises(invitations.InviteConfigurationError):
        invitations.normalize_entitlement_policy(policy)
    policy["fallback_policy"] = {"enabled": False, "order": []}
    policy["max_concurrent"] = 99
    with pytest.raises(invitations.InviteConfigurationError):
        invitations.normalize_entitlement_policy(policy)


def test_target_refs_are_domain_separated_and_contain_no_identity():
    config = invitations.InvitationConfig(hmac_pepper=b"unit-test-only")
    account_ref, profile_ref = invitations.invitation_target_refs(
        config,
        account_uid="account-a",
        profile_uid="account-a",
    )

    assert len(account_ref) == len(profile_ref) == 64
    assert account_ref != profile_ref
    assert "account-a" not in account_ref + profile_ref


def test_pilot_gate_requires_exact_v8_consent_and_all_uid_allowlists(
    monkeypatch,
):
    uid = "synthetic-pilot"
    repository = ai_consent.InMemoryConsentRepository()
    monkeypatch.setattr(ai_consent, "_repository", repository)
    monkeypatch.setenv("ELLA_HERMES_CLOUD_SYNTHETIC_ONLY", "true")
    for name in invitation_authority.PILOT_UID_ALLOWLISTS:
        monkeypatch.setenv(name, uid)
    for name in invitation_authority.PILOT_GLOBAL_FLAGS_REQUIRED_FALSE:
        monkeypatch.setenv(name, "false")

    with pytest.raises(invitations.InvitePilotGateDenied):
        invitation_authority.authorize_invitation_pilot(uid)

    ai_consent.AiConsentService(repository).submit(
        uid,
        ai_consent.ConsentSubmission(
            decision="granted",
            policy_version=ai_consent.CURRENT_POLICY_VERSION,
            processor_set_hash=ai_consent.CURRENT_PROCESSOR_SET_HASH,
            request_id="synthetic-pilot-consent",
            app_version="synthetic",
            build_number="1",
            locale="en",
            scope_version=ai_consent.CURRENT_SCOPE_VERSION,
            scope_hash=ai_consent.CURRENT_SCOPE_HASH,
        ),
    )
    admission = invitation_authority.authorize_invitation_pilot(uid)
    assert admission.policy_version == "ai-data-processors-v8"
    assert admission.account_uid == admission.profile_uid == uid

    monkeypatch.setenv("ELLA_RUNTIME_BINDINGS_ENABLED_UIDS", "")
    with pytest.raises(invitations.InvitePilotGateDenied):
        invitation_authority.authorize_invitation_pilot(uid)

    monkeypatch.setenv("ELLA_RUNTIME_BINDINGS_ENABLED_UIDS", uid)
    monkeypatch.setenv("ELLA_RUNTIME_BINDINGS_ENABLED", "true")
    with pytest.raises(invitations.InvitePilotGateDenied):
        invitation_authority.authorize_invitation_pilot(uid)


def test_source_address_trusts_forwarding_only_from_enumerated_proxy(monkeypatch):
    monkeypatch.setenv("ELLA_INVITE_TRUSTED_PROXY_IPS", "127.0.0.1")
    headers = [(b"x-forwarded-for", b"198.51.100.9, 127.0.0.1")]
    assert invites._source_address(_request(client="127.0.0.1", headers=headers)) == "198.51.100.9"
    assert invites._source_address(_request(client="192.0.2.7", headers=headers)) == "192.0.2.7"


def test_routes_require_auth_and_use_only_authenticated_uid(monkeypatch):
    app = FastAPI()
    app.include_router(invites.router)
    client = TestClient(app)
    assert client.post("/v1/invite/redeem", json={"code": "ABCD-2345"}).status_code == 401

    captured = {}

    async def fake_redeem(**kwargs):
        captured.update(kwargs)
        return {
            "status": "invited",
            "quota": {},
            "support_code": "INV-1234ABCD",
            "correlation_id": "11111111-1111-1111-1111-111111111111",
        }

    monkeypatch.setattr(
        invites,
        "authorize_invitation_pilot",
        lambda uid: invitations.InvitationPilotAdmission(
            account_uid=uid,
            profile_uid=uid,
            consent_receipt_id="synthetic-receipt",
            profile_binding_id=ai_consent.derive_profile_binding_id(
                account_uid=uid,
                profile_uid=uid,
            ),
            policy_version=ai_consent.CURRENT_POLICY_VERSION,
            processor_set_hash=ai_consent.CURRENT_PROCESSOR_SET_HASH,
            scope_version=ai_consent.CURRENT_SCOPE_VERSION,
            scope_hash=ai_consent.CURRENT_SCOPE_HASH,
        ),
    )
    monkeypatch.setattr(invites.invitations, "redeem_invitation", fake_redeem)
    monkeypatch.setattr(
        invites.auth,
        "get_user",
        lambda _uid: SimpleNamespace(email="verified@example.test", email_verified=True),
    )
    app.dependency_overrides[auth.get_writable_user_uid] = lambda: "firebase-subject"
    response = client.post(
        "/v1/invite/redeem",
        json={"code": "ABCD-2345"},
        headers={"X-Ella-App-Build": "804"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "invited"
    assert captured["uid"] == "firebase-subject"
    assert captured["app_build"] == "804"
    assert captured["pilot_admission_revalidator"] is invites.revalidate_invitation_pilot


def test_redeem_rejects_unverified_firebase_email_before_authority_or_database(monkeypatch):
    app = FastAPI()
    app.include_router(invites.router)
    app.dependency_overrides[auth.get_writable_user_uid] = lambda: "firebase-subject"
    monkeypatch.setattr(
        invites.auth,
        "get_user",
        lambda _uid: SimpleNamespace(email="unverified@example.test", email_verified=False),
    )
    monkeypatch.setattr(
        invites,
        "authorize_invitation_pilot",
        lambda _uid: (_ for _ in ()).throw(AssertionError("authority must not run")),
    )
    monkeypatch.setattr(
        invites.invitations,
        "redeem_invitation",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("database must not run")),
    )

    response = TestClient(app).post("/v1/invite/redeem", json={"code": "ABCD-2345"})

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid"


@pytest.mark.parametrize(
    ("payload", "secret_marker"),
    [
        pytest.param({}, None, id="missing-code"),
        pytest.param(
            {"code": {"secret": "wrong-type-secret"}},
            "wrong-type-secret",
            id="wrong-type",
        ),
        pytest.param(
            {"code": "overlength-secret-" + ("X" * 32)},
            "overlength-secret",
            id="overlength",
        ),
        pytest.param(
            {"code": "ABCD-2345", "uid": "extra-field-secret"},
            "extra-field-secret",
            id="extra-field",
        ),
    ],
)
def test_authenticated_malformed_body_returns_safe_invalid_envelope(
    monkeypatch,
    payload,
    secret_marker,
):
    app = FastAPI()
    app.include_router(invites.router)
    app.dependency_overrides[auth.get_writable_user_uid] = lambda: "firebase-subject"
    redemption_called = False

    async def fake_redeem(**_kwargs):
        nonlocal redemption_called
        redemption_called = True
        return {}

    monkeypatch.setattr(invites.invitations, "redeem_invitation", fake_redeem)
    response = TestClient(app).post("/v1/invite/redeem", json=payload)

    assert response.status_code == 400
    assert not redemption_called
    assert set(response.json()) == {"detail"}
    detail = response.json()["detail"]
    assert set(detail) == {"code", "support_code", "correlation_id"}
    assert detail["code"] == "invalid"
    assert detail["support_code"].startswith("INV-")
    assert len(detail["support_code"]) == 12
    assert str(UUID(detail["correlation_id"])) == detail["correlation_id"]
    if secret_marker:
        assert secret_marker not in response.text


def test_unauthenticated_malformed_body_remains_unauthorized():
    app = FastAPI()
    app.include_router(invites.router)

    response = TestClient(app).post(
        "/v1/invite/redeem",
        json={"code": "overlength-secret-" + ("X" * 32)},
    )

    assert response.status_code == 401


def test_entitlement_read_requires_auth_and_is_uid_scoped(monkeypatch):
    app = FastAPI()
    app.include_router(voice.entitlement_router)
    client = TestClient(app)
    assert client.get("/v1/entitlement").status_code == 401

    captured = {}

    async def fake_contract(uid):
        captured["uid"] = uid
        return {"status": "none", "quota": {}}

    monkeypatch.setattr(voice.voice_canary_db, "get_entitlement_contract", fake_contract)
    app.dependency_overrides[auth.get_writable_user_uid] = lambda: "firebase-subject"
    response = client.get("/v1/entitlement")
    assert response.status_code == 200
    assert captured["uid"] == "firebase-subject"
    assert response.json()["support_code"].startswith("ENT-")
    assert len(response.json()["correlation_id"]) == 36


def test_typed_failure_shape_is_compatible_with_ios(monkeypatch):
    app = FastAPI()
    app.include_router(invites.router)
    app.dependency_overrides[auth.get_writable_user_uid] = lambda: "firebase-subject"

    async def fake_redeem(**_kwargs):
        raise invitations.InviteRedemptionFailure(
            "rate_limited",
            status_code=429,
            support_code="INV-1234ABCD",
            correlation_id="11111111-1111-1111-1111-111111111111",
            retry_after_s=42,
        )

    monkeypatch.setattr(
        invites,
        "authorize_invitation_pilot",
        lambda uid: invitations.InvitationPilotAdmission(
            account_uid=uid,
            profile_uid=uid,
            consent_receipt_id="synthetic-receipt",
            profile_binding_id=ai_consent.derive_profile_binding_id(
                account_uid=uid,
                profile_uid=uid,
            ),
            policy_version=ai_consent.CURRENT_POLICY_VERSION,
            processor_set_hash=ai_consent.CURRENT_PROCESSOR_SET_HASH,
            scope_version=ai_consent.CURRENT_SCOPE_VERSION,
            scope_hash=ai_consent.CURRENT_SCOPE_HASH,
        ),
    )
    monkeypatch.setattr(invites.invitations, "redeem_invitation", fake_redeem)
    monkeypatch.setattr(
        invites.auth,
        "get_user",
        lambda _uid: SimpleNamespace(email="verified@example.test", email_verified=True),
    )
    response = TestClient(app).post("/v1/invite/redeem", json={"code": "ABCD-2345"})

    assert response.status_code == 429
    assert response.json() == {
        "detail": {
            "code": "rate_limited",
            "support_code": "INV-1234ABCD",
            "correlation_id": "11111111-1111-1111-1111-111111111111",
            "retry_after_s": 42,
        }
    }


def test_migration_has_privacy_capacity_and_app_review_guards():
    migration = (
        (Path(__file__).resolve().parents[2] / "migrations" / "011_create_invitation_redemption.sql")
        .read_text(encoding="utf-8")
        .lower()
    )

    for fragment in (
        "code_hmac char(64)",
        "uid_ref_hmac char(64)",
        "source_ref_hmac char(64)",
        "ella_invitation_capacity_reservations",
        "ella_invitation_targets",
        "account_ref_hmac char(64)",
        "profile_ref_hmac char(64)",
        "consent_receipt_ref_hmac char(64)",
        "required_profile_class = 'synthetic'",
        "required_consent_policy_version",
        "required_consent_processor_set_hash",
        "required_consent_scope_version",
        "required_consent_scope_hash",
        "ella_invitation_redemptions_invite_uid_key",
        "ella_invitation_redemptions_target_key",
        "max_redemptions <= 20",
        "reserved_setup_slots = 2",
        "exclude_from_product_analytics = true",
        "add column if not exists invitation_id",
    ):
        assert fragment in migration
    assert "raw_ip" not in migration
    assert "plaintext_code" not in migration
