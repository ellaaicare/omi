import importlib.util
import asyncio
import sys
import types
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

sys.modules.setdefault("asyncpg", types.SimpleNamespace(Pool=object, Connection=object, create_pool=None))
firebase_admin = types.ModuleType("firebase_admin")
firebase_auth = types.ModuleType("firebase_admin.auth")


class InvalidIdTokenError(Exception):
    pass


firebase_auth.InvalidIdTokenError = InvalidIdTokenError
sys.modules.setdefault("firebase_admin", firebase_admin)
sys.modules.setdefault("firebase_admin.auth", firebase_auth)

_ROUTER_PATH = Path(__file__).resolve().parents[2] / "ella" / "routers" / "escalations.py"


def _load_escalations_module(module_name):
    spec = importlib.util.spec_from_file_location(module_name, _ROUTER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


escalations = _load_escalations_module("ella_escalations_under_test")

_TEST_ESCALATION_WEBHOOK_KEY = "test-only-escalation-authority-key"


@pytest.fixture
def configured_escalation_key(monkeypatch):
    monkeypatch.setattr(escalations, "ESCALATION_WEBHOOK_KEY", _TEST_ESCALATION_WEBHOOK_KEY)
    return _TEST_ESCALATION_WEBHOOK_KEY


def _policy_context(uid):
    return (
        escalations.UserPolicyContext(
            uid=uid,
            guardian_mode="memory_support",
            user_email="user@example.test",
            user_phone="+15550000001",
        ),
        [
            escalations.CaregiverPolicyContext(
                caregiver_id="caregiver-1",
                status="ACTIVE",
                is_emergency_contact=True,
                name="Emily",
                email="caregiver@example.test",
                phone="+15550000002",
            )
        ],
    )


def test_policy_view_internal_key_reaches_load_context_for_explicit_uid(monkeypatch, configured_escalation_key):
    loaded_uids = []

    async def fake_load_context(uid):
        loaded_uids.append(uid)
        return _policy_context(uid)

    monkeypatch.setattr(escalations, "_load_context", fake_load_context)

    response = asyncio.run(
        escalations.get_escalation_policy(
            uid="uid-1",
            authorization=None,
            x_guardian_key=_TEST_ESCALATION_WEBHOOK_KEY,
            x_escalation_key=None,
            key=None,
        )
    )

    assert loaded_uids == ["uid-1"]
    assert response["ok"] is True
    assert response["uid"] == "uid-1"


def test_policy_view_internal_key_requires_uid_before_load_context(monkeypatch, configured_escalation_key):
    async def fail_load_context(_uid):
        raise AssertionError("_load_context must not run without an explicit service-authority UID")

    monkeypatch.setattr(escalations, "_load_context", fail_load_context)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            escalations.get_escalation_policy(
                uid=None,
                authorization=None,
                x_guardian_key=_TEST_ESCALATION_WEBHOOK_KEY,
                x_escalation_key=None,
                key=None,
            )
        )

    assert exc.value.status_code == 400


def test_policy_view_uid_uses_authenticated_uid_when_uid_omitted(monkeypatch):
    monkeypatch.setattr(escalations.auth_endpoints, "verify_token", lambda token: "uid-1")

    assert (
        escalations._resolve_policy_view_uid(
            None,
            authorization="Bearer token-1",
            x_guardian_key=None,
            x_escalation_key=None,
            key=None,
        )
        == "uid-1"
    )


def test_policy_view_uid_rejects_cross_user_read(monkeypatch):
    monkeypatch.setattr(escalations.auth_endpoints, "verify_token", lambda token: "uid-1")

    with pytest.raises(HTTPException) as exc:
        escalations._resolve_policy_view_uid(
            "uid-2",
            authorization="Bearer token-1",
            x_guardian_key=None,
            x_escalation_key=None,
            key=None,
        )

    assert exc.value.status_code == 403


def test_policy_view_denies_absent_empty_malformed_and_wrong_credentials_before_context(
    monkeypatch, configured_escalation_key
):
    async def fail_load_context(_uid):
        raise AssertionError("_load_context must not run for denied authority")

    verified_tokens = []

    def reject_firebase_token(token):
        verified_tokens.append(token)
        raise InvalidIdTokenError

    monkeypatch.setattr(escalations, "_load_context", fail_load_context)
    monkeypatch.setattr(escalations.auth_endpoints, "verify_token", reject_firebase_token)

    denial_cases = {
        "absent": {},
        "raw-empty-guardian-key": {"x_guardian_key": ""},
        "raw-empty-escalation-key": {"x_escalation_key": ""},
        "raw-empty-legacy-key": {"key": ""},
        "malformed-service-key": {"x_escalation_key": f" {_TEST_ESCALATION_WEBHOOK_KEY} "},
        "wrong-service-key": {"x_guardian_key": "wrong-test-only-key"},
        "malformed-firebase-authorization": {"authorization": "not-a-bearer-token"},
        "invalid-firebase-token": {"authorization": "Bearer invalid-test-token"},
    }

    for case_name, overrides in denial_cases.items():
        arguments = {
            "uid": "uid-1",
            "authorization": None,
            "x_guardian_key": None,
            "x_escalation_key": None,
            "key": None,
            **overrides,
        }
        with pytest.raises(HTTPException) as exc:
            asyncio.run(escalations.get_escalation_policy(**arguments))
        assert exc.value.status_code == 401, case_name

    assert verified_tokens == ["invalid-test-token"]


class _FakePool:
    def __init__(self, user_row):
        self.user_row = user_row
        self.fetchrow_queries = []

    async def fetchrow(self, query, *args):
        self.fetchrow_queries.append((query, args))
        return self.user_row

    async def fetch(self, *_args):
        return []


def test_load_context_uses_canonical_phone_number_for_user_imessage(monkeypatch):
    pool = _FakePool(
        {
            "id": "user-1",
            "omi_uid": "canonical-uid",
            "guardian_mode": "off",
            "email": "user@example.test",
            "phone_number": "+15550000001",
            "identities": {},
        }
    )
    monkeypatch.setattr(
        escalations,
        "_pool",
        pool,
    )

    user, caregivers = asyncio.run(escalations._load_context("canonical-uid"))
    policy = escalations.build_plain_language_policy_view(user, caregivers)

    assert user.user_phone == "+15550000001"
    assert policy["user"]["channels"][1] == {
        "channel": "imessage",
        "enabled": True,
        "reason": "Phone number on file",
    }
    assert pool.fetchrow_queries[0][1] == ("canonical-uid",)
    assert "WHERE omi_uid = $1" in pool.fetchrow_queries[0][0]
    assert "lower(omi_uid)" not in pool.fetchrow_queries[0][0].lower()


def test_load_context_allows_identities_phone_override(monkeypatch):
    monkeypatch.setattr(
        escalations,
        "_pool",
        _FakePool(
            {
                "id": "user-1",
                "omi_uid": "canonical-uid",
                "guardian_mode": "off",
                "email": "user@example.test",
                "phone_number": "+15550000001",
                "identities": {"phone": "+15550000099"},
            }
        ),
    )

    user, _caregivers = asyncio.run(escalations._load_context("canonical-uid"))

    assert user.user_phone == "+15550000099"


def test_policy_markdown_internal_key_reaches_same_auth_and_context(monkeypatch, configured_escalation_key):
    loaded_uids = []

    async def fake_load_context(uid):
        loaded_uids.append(uid)
        return _policy_context(uid)

    monkeypatch.setattr(escalations, "_load_context", fake_load_context)

    response = asyncio.run(
        escalations.get_escalation_policy_markdown(
            uid="uid-1",
            authorization=None,
            x_guardian_key=_TEST_ESCALATION_WEBHOOK_KEY,
            x_escalation_key=None,
            key=None,
        )
    )

    assert loaded_uids == ["uid-1"]
    assert response.media_type == "text/markdown; charset=utf-8"
    body = response.body.decode("utf-8")
    assert "- uid: `uid-1`" in body
    assert "- guardian_mode: `memory_support`" in body
    assert "`direct_user_request`" in body
    assert "`critical_safety`" in body


def test_evaluate_internal_key_reaches_load_context_for_explicit_uid(monkeypatch, configured_escalation_key):
    loaded_uids = []

    async def fake_load_context(uid):
        loaded_uids.append(uid)
        return _policy_context(uid)

    async def fake_log_decision(_uid, _decision):
        return None

    monkeypatch.setattr(escalations, "_load_context", fake_load_context)
    monkeypatch.setattr(escalations, "_log_decision", fake_log_decision)

    response = asyncio.run(
        escalations.evaluate_escalation(
            escalations.EscalationEvaluateRequest(uid="uid-1"),
            x_guardian_key=None,
            x_escalation_key=_TEST_ESCALATION_WEBHOOK_KEY,
            key=None,
        )
    )

    assert loaded_uids == ["uid-1"]
    assert response["ok"] is True
    assert response["trace_id"] == "uid-1"


def test_production_default_raw_empty_legacy_key_denies_all_authority_paths_before_context(monkeypatch):
    monkeypatch.delenv("ELLA_ESCALATION_WEBHOOK_KEY", raising=False)
    monkeypatch.delenv("GUARDIAN_WEBHOOK_KEY", raising=False)
    production_escalations = _load_escalations_module("ella_escalations_production_default_under_test")
    assert production_escalations.ESCALATION_WEBHOOK_KEY == ""

    async def fail_load_context(_uid):
        raise AssertionError("_load_context must not run for an unconfigured service authority")

    monkeypatch.setattr(production_escalations, "_load_context", fail_load_context)
    app = FastAPI()
    app.include_router(production_escalations.router)

    with TestClient(app) as client:
        policy_response = client.get(
            "/v1/ella/escalations/policy",
            params={"uid": "uid-1"},
            headers={"X-Key": ""},
        )
        markdown_response = client.get(
            "/v1/ella/escalations/policy.md",
            params={"uid": "uid-1"},
            headers={"X-Key": ""},
        )
        evaluate_response = client.post(
            "/v1/ella/escalations/evaluate",
            headers={"X-Key": ""},
            json={"uid": "uid-1"},
        )

    assert policy_response.status_code == 401
    assert markdown_response.status_code == 401
    assert evaluate_response.status_code == 403

    with pytest.raises(HTTPException) as exc:
        production_escalations._verify_key(None, None, "")
    assert exc.value.status_code == 403

    monkeypatch.setattr(production_escalations, "ESCALATION_WEBHOOK_KEY", " \t")
    with pytest.raises(HTTPException) as exc:
        production_escalations._verify_key(None, None, " \t")
    assert exc.value.status_code == 403
