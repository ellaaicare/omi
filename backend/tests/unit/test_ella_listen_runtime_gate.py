import asyncio
from types import SimpleNamespace

from ella.routers import auto_provision
from ella.services.provisioning import ProvisioningError


def test_isolated_listen_accepts_owned_runtime_and_existing_omi_user(monkeypatch):
    async def resolve(uid):
        return SimpleNamespace(uid=uid)

    monkeypatch.setattr("ella.services.runtime_resolver.resolve_isolated_runtime", resolve)
    result = asyncio.run(auto_provision.validate_isolated_listen_runtime("user-a", lambda uid: uid == "user-a"))

    assert result == {"success": True, "provider": "hermes"}


def test_isolated_listen_never_falls_back_when_binding_is_missing(monkeypatch):
    async def resolve(_uid):
        raise ProvisioningError("hermes_not_provisioned", retryable=True)

    monkeypatch.setattr("ella.services.runtime_resolver.resolve_isolated_runtime", resolve)
    monkeypatch.setattr(
        auto_provision,
        "auto_provision_user",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("OpenClaw fallback invoked")),
    )

    result = asyncio.run(auto_provision.validate_isolated_listen_runtime("user-a", lambda _uid: True))

    assert result == {"success": False, "error": "hermes_not_provisioned"}


def test_isolated_listen_requires_omi_identity(monkeypatch):
    async def resolve(uid):
        return SimpleNamespace(uid=uid)

    monkeypatch.setattr("ella.services.runtime_resolver.resolve_isolated_runtime", resolve)
    result = asyncio.run(auto_provision.validate_isolated_listen_runtime("user-a", lambda _uid: False))

    assert result == {"success": False, "error": "omi_identity_unavailable"}


def test_listen_runtime_gate_skips_legacy_users(monkeypatch):
    monkeypatch.setattr(auto_provision.runtime_resolver, "runtime_bindings_enabled", lambda _uid: False)
    monkeypatch.setattr(
        auto_provision,
        "validate_isolated_listen_runtime",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected validation")),
    )

    result = asyncio.run(auto_provision.listen_runtime_gate("legacy-user", lambda _uid: True))

    assert result == {"required": False, "success": True}


def test_listen_runtime_gate_fails_closed_for_isolated_users(monkeypatch):
    monkeypatch.setattr(auto_provision.runtime_resolver, "runtime_bindings_enabled", lambda _uid: True)

    async def validate(uid, checker):
        assert uid == "isolated-user"
        assert checker(uid) is True
        return {"success": False, "error": "hermes_not_provisioned"}

    monkeypatch.setattr(auto_provision, "validate_isolated_listen_runtime", validate)

    result = asyncio.run(auto_provision.listen_runtime_gate("isolated-user", lambda _uid: True))

    assert result == {
        "required": True,
        "success": False,
        "error": "hermes_not_provisioned",
    }


def test_legacy_firestore_repair_requests_historical_cloud_sync_default(monkeypatch):
    captured = {}

    class FakePool:
        async def fetchrow(self, _query, uid):
            assert uid == "legacy-user"
            return {
                "name": "Legacy User",
                "email": "legacy@example.com",
                "timezone": "America/Los_Angeles",
            }

    class FakeRepository:
        def __init__(self, pool, *, firestore_db):
            assert isinstance(pool, FakePool)
            assert firestore_db == "firestore"

        async def ensure_omi_user_document(self, **kwargs):
            captured.update(kwargs)
            return True

    async def get_pool():
        return FakePool()

    monkeypatch.setattr(auto_provision, "_get_pool", get_pool)
    monkeypatch.setattr(auto_provision, "EllaProvisioningRepository", FakeRepository)

    result = asyncio.run(auto_provision.ensure_firestore_user_document("legacy-user", "firestore"))

    assert result is True
    assert captured["private_cloud_sync_default"] is True
