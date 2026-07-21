import asyncio
from types import SimpleNamespace

from ella.routers import auto_provision
from ella.services.provisioning import ProvisioningError


def test_isolated_listen_accepts_owned_runtime_and_existing_omi_user(monkeypatch):
    async def resolve(uid):
        return SimpleNamespace(uid=uid)

    monkeypatch.setattr("ella.services.runtime_resolver.resolve_isolated_runtime", resolve)
    result = asyncio.run(
        auto_provision.validate_isolated_listen_runtime("user-a", lambda uid: uid == "user-a")
    )

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
