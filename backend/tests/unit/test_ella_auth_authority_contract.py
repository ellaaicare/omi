import ast
import logging
from pathlib import Path

import pytest
from fastapi import HTTPException
from firebase_admin.auth import InvalidIdTokenError

from utils.ella import exact_firebase_auth
from utils.other import endpoints

BACKEND = Path(__file__).resolve().parents[2]


def _reject_firebase(_token):
    raise InvalidIdTokenError("not firebase")


def test_admin_key_suffix_cannot_mint_arbitrary_subject(monkeypatch):
    monkeypatch.setenv("ADMIN_KEY", "unit-admin-key:")
    monkeypatch.delenv("ELLA_ADMIN_SUBJECT_ALLOWLIST", raising=False)
    monkeypatch.setattr(endpoints.auth, "verify_id_token", _reject_firebase)

    with pytest.raises(InvalidIdTokenError):
        endpoints.verify_token("unit-admin-key:attacker", caller_class="http")

    monkeypatch.setenv("ELLA_ADMIN_SUBJECT_ALLOWLIST", "controlled-user")
    assert endpoints.verify_token("unit-admin-key:controlled-user", caller_class="http") == "controlled-user"
    with pytest.raises(InvalidIdTokenError):
        endpoints.verify_token("unit-admin-key:controlled-user-extra", caller_class="http")


def test_admin_branch_observation_is_bounded_and_content_free(monkeypatch, caplog):
    monkeypatch.setenv("ADMIN_KEY", "unit-admin-key:")
    monkeypatch.delenv("ELLA_ADMIN_SUBJECT_ALLOWLIST", raising=False)
    monkeypatch.setattr(endpoints.auth, "verify_id_token", _reject_firebase)
    endpoints._ADMIN_BRANCH_COUNTS.clear()

    with caplog.at_level(logging.INFO, logger=endpoints.__name__):
        with pytest.raises(InvalidIdTokenError):
            endpoints.verify_token("unit-admin-key:sensitive-subject", caller_class="http")

    record = caplog.records[-1]
    assert record.caller_class == "http"
    assert record.invocation_count == 1
    assert record.invocation_count <= endpoints._ADMIN_BRANCH_COUNT_LIMIT
    assert "sensitive-subject" not in record.getMessage()
    assert set(record.__dict__) & {"token", "uid", "headers", "content"} == set()


@pytest.mark.parametrize("marker", ["ELLA_PRODUCTION", "PRODUCTION", "ENVIRONMENT", "DD_ENV"])
def test_production_marker_disables_local_development_subject_fallback(monkeypatch, marker):
    monkeypatch.delenv("ADMIN_KEY", raising=False)
    monkeypatch.setenv("LOCAL_DEVELOPMENT", "true")
    monkeypatch.setenv(marker, "production")
    monkeypatch.setattr(endpoints.auth, "verify_id_token", _reject_firebase)

    with pytest.raises(InvalidIdTokenError):
        endpoints.verify_token("not-firebase")


def test_nonproduction_local_development_fallback_remains_available(monkeypatch):
    monkeypatch.delenv("ADMIN_KEY", raising=False)
    monkeypatch.setenv("LOCAL_DEVELOPMENT", "true")
    for marker in ("ELLA_PRODUCTION", "PRODUCTION", "ENVIRONMENT", "DD_ENV"):
        monkeypatch.delenv(marker, raising=False)
    monkeypatch.setattr(endpoints.auth, "verify_id_token", _reject_firebase)

    assert endpoints.verify_token("not-firebase") == "123"


def test_exact_firebase_boundary_never_accepts_admin_key(monkeypatch):
    monkeypatch.setenv("ADMIN_KEY", "unit-admin-key:")
    monkeypatch.setenv("ELLA_ADMIN_SUBJECT_ALLOWLIST", "controlled-user")
    monkeypatch.setattr(exact_firebase_auth.firebase_auth, "verify_id_token", _reject_firebase)

    with pytest.raises(HTTPException) as error:
        exact_firebase_auth.get_exact_firebase_uid("Bearer unit-admin-key:controlled-user")

    assert error.value.status_code == 401


def test_ella_lazy_exports_keep_imports_at_module_scope():
    source = (BACKEND / "utils" / "ella" / "__init__.py").read_text()
    tree = ast.parse(source)
    lazy_loader = next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "__getattr__"
    )

    assert not any(isinstance(node, (ast.Import, ast.ImportFrom)) for node in ast.walk(lazy_loader))


def test_minimum_client_build_is_fail_closed_and_version_aware(monkeypatch):
    monkeypatch.setattr(exact_firebase_auth.firebase_auth, "verify_id_token", lambda _token: {"uid": "user-a"})
    monkeypatch.setenv("ELLA_MIN_SUPPORTED_CLIENT_BUILD", "805")

    with pytest.raises(HTTPException) as missing:
        exact_firebase_auth.get_exact_firebase_uid("Bearer firebase", None, None, None)
    assert missing.value.status_code == 426

    with pytest.raises(HTTPException) as old:
        exact_firebase_auth.get_exact_firebase_uid("Bearer firebase", None, "804", None)
    assert old.value.status_code == 426
    assert exact_firebase_auth.get_exact_firebase_uid("Bearer firebase", None, "805", None) == "user-a"
    with pytest.raises(HTTPException) as old_shared_header:
        exact_firebase_auth.get_exact_firebase_uid("Bearer firebase", "1.0.525+804", None, None)
    assert old_shared_header.value.status_code == 426
    assert exact_firebase_auth.get_exact_firebase_uid("Bearer firebase", "1.0.525+805", None, None) == "user-a"
    with pytest.raises(HTTPException) as missing_build_component:
        exact_firebase_auth.get_exact_firebase_uid("Bearer firebase", "1.0.525", None, None)
    assert missing_build_component.value.status_code == 426

    monkeypatch.setenv("ELLA_MIN_SUPPORTED_CLIENT_BUILD", "1.0.525")
    assert exact_firebase_auth.get_exact_firebase_uid("Bearer firebase", "1.0.525", None, None) == "user-a"
    assert exact_firebase_auth.get_exact_firebase_uid("Bearer firebase", "1.0.525+805", None, None) == "user-a"
    with pytest.raises(HTTPException) as build_only_for_semantic_gate:
        exact_firebase_auth.get_exact_firebase_uid("Bearer firebase", None, "805", None)
    assert build_only_for_semantic_gate.value.status_code == 426
    with pytest.raises(HTTPException) as old_version:
        exact_firebase_auth.get_exact_firebase_uid("Bearer firebase", "1.0.524+999", None, None)
    assert old_version.value.status_code == 426

    monkeypatch.setenv("ELLA_MIN_SUPPORTED_CLIENT_BUILD", "not-a-version")
    with pytest.raises(HTTPException) as invalid_configuration:
        exact_firebase_auth.get_exact_firebase_uid("Bearer firebase", None, "805", None)
    assert invalid_configuration.value.status_code == 503


def test_service_authority_is_constant_time_bound_to_one_subject():
    authority = exact_firebase_auth.get_exact_service_authority(
        provided_service_key="service-secret",
        configured_service_key="service-secret",
        service_subject_uid="user-a",
        service="test_service",
    )
    assert authority.require_uid("user-a", feature="test") == "user-a"
    with pytest.raises(HTTPException) as cross_owner:
        authority.require_uid("user-b", feature="test")
    assert cross_owner.value.status_code == 403

    with pytest.raises(HTTPException) as unbound:
        exact_firebase_auth.get_exact_service_authority(
            provided_service_key="service-secret",
            configured_service_key="service-secret",
            service_subject_uid=None,
            service="test_service",
        )
    assert unbound.value.status_code == 403


def test_ella_user_routes_do_not_inherit_legacy_admin_subject_auth():
    sources = [
        *(BACKEND / "ella" / "routers").glob("*.py"),
        *(BACKEND / "ella" / "services").glob("*.py"),
    ]
    assert not any("auth.get_current_user_uid" in path.read_text(encoding="utf-8") for path in sources)


def test_fifteen_fixed_capability_admin_guards_are_retained():
    assert (BACKEND / "routers" / "apps.py").read_text(encoding="utf-8").count("os.getenv('ADMIN_KEY')") == 12
    assert (BACKEND / "routers" / "notifications.py").read_text(encoding="utf-8").count("os.getenv('ADMIN_KEY')") == 1
    assert (BACKEND / "routers" / "updates.py").read_text(encoding="utf-8").count("os.getenv('ADMIN_KEY')") == 1
    assert (BACKEND / "routers" / "announcements.py").read_text(encoding="utf-8").count('os.getenv("ADMIN_KEY")') == 1
