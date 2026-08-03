import importlib.util
import asyncio
import sys
import types
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.modules.setdefault("asyncpg", types.SimpleNamespace(Pool=object, Connection=object, create_pool=None))
firebase_admin = types.ModuleType("firebase_admin")
firebase_auth = types.ModuleType("firebase_admin.auth")


class InvalidIdTokenError(Exception):
    pass


firebase_auth.InvalidIdTokenError = InvalidIdTokenError
sys.modules.setdefault("firebase_admin", firebase_admin)
sys.modules.setdefault("firebase_admin.auth", firebase_auth)

_ROUTER_PATH = Path(__file__).resolve().parents[2] / "ella" / "routers" / "escalations.py"
_SPEC = importlib.util.spec_from_file_location("ella_escalations_under_test", _ROUTER_PATH)
escalations = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(escalations)


def test_policy_view_uid_allows_internal_key_with_explicit_uid():
    assert (
        escalations._resolve_policy_view_uid(
            "uid-1",
            authorization=None,
            x_guardian_key=escalations.ESCALATION_WEBHOOK_KEY,
            x_escalation_key=None,
            key=None,
        )
        == "uid-1"
    )


def test_policy_view_uid_requires_uid_for_internal_key():
    with pytest.raises(HTTPException) as exc:
        escalations._resolve_policy_view_uid(
            None,
            authorization=None,
            x_guardian_key=escalations.ESCALATION_WEBHOOK_KEY,
            x_escalation_key=None,
            key=None,
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


def test_policy_view_uid_requires_authorization_without_internal_key():
    with pytest.raises(HTTPException) as exc:
        escalations._resolve_policy_view_uid(
            "uid-1",
            authorization=None,
            x_guardian_key=None,
            x_escalation_key=None,
            key=None,
        )

    assert exc.value.status_code == 401


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


def test_policy_markdown_endpoint_uses_same_auth_and_context(monkeypatch):
    async def fake_load_context(uid):
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

    monkeypatch.setattr(escalations, "_load_context", fake_load_context)

    response = asyncio.run(
        escalations.get_escalation_policy_markdown(
            uid="uid-1",
            authorization=None,
            x_guardian_key=escalations.ESCALATION_WEBHOOK_KEY,
            x_escalation_key=None,
            key=None,
        )
    )

    assert response.media_type == "text/markdown; charset=utf-8"
    body = response.body.decode("utf-8")
    assert "- uid: `uid-1`" in body
    assert "- guardian_mode: `memory_support`" in body
    assert "`direct_user_request`" in body
    assert "`critical_safety`" in body
