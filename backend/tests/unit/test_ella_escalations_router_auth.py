import importlib.util
import sys
import types
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.modules.setdefault("asyncpg", types.SimpleNamespace(Pool=object, create_pool=None))
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
