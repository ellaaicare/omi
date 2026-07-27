import asyncio
from types import SimpleNamespace

from ella.services.hermes_cloud import HermesCloudPreflight
from ella.services.provisioning import (
    HermesProvisionClient,
    ProvisioningCoordinator,
    ProvisioningError,
    VerifiedIdentity,
)


class ForbiddenLocalClient(HermesProvisionClient):
    async def provision(self, identity, target_schema_version):
        raise AssertionError("cloud provisioning must not call the Mini provision shim")


class FakeRepository:
    def __init__(self, *, pool_empty=False):
        self.pool_empty = pool_empty
        self.quarantined = []
        self.finalized = []
        self.jobs = []
        self.claims = 0
        self.delivered_alerts = []

    async def claim_cloud_pool_binding(self, **kwargs):
        self.claims += 1
        if self.pool_empty:
            return None
        return {
            "id": "binding-a",
            "claim_token": "00000000-0000-0000-0000-000000000004",
            "expected_model": "model-a",
            "api_base_url_ref": "env:ELLA_HERMES_CLOUD_API_URL_SYNTHETIC",
            "api_key_ref": "env:ELLA_HERMES_CLOUD_API_KEY_SYNTHETIC",
            "honcho_api_key_ref": "env:ELLA_HONCHO_CLOUD_API_KEY_SYNTHETIC",
        }

    async def reconcile_cloud_pool_alert(self, **kwargs):
        return {
            "available": 1 if not self.pool_empty else 0,
            "alert": {"id": "alert-a", "threshold": kwargs["threshold"]},
        }

    async def finalize_cloud_pool_claim(self, **kwargs):
        self.finalized.append(kwargs)
        return {
            "revision": 2,
            "runtime_instance_id": "instance-a",
        }

    async def mark_cloud_pool_alert_delivered(self, alert_id):
        self.delivered_alerts.append(alert_id)

    async def quarantine_cloud_pool_claim(self, **kwargs):
        self.quarantined.append(kwargs)

    async def update_job(self, **kwargs):
        self.jobs.append(kwargs)
        return kwargs


class FakeCloud:
    def __init__(self, *, error=None):
        self.error = error
        self.calls = []

    async def preflight(self, binding):
        self.calls.append(binding)
        if self.error:
            raise self.error
        return HermesCloudPreflight(
            model="model-a",
            tools=(),
            capabilities=("responses_api", "session_key_header"),
            receipt={"status": "ok", "content_free": True},
        )


class FakeHoncho:
    def __init__(self):
        self.calls = []

    async def ensure_profile(self, binding):
        self.calls.append(binding)
        return {
            "workspace": "workspace-a",
            "observed_peer": "user-a",
            "observer_peer": "companion-a",
        }


class FakeAlert:
    def __init__(self):
        self.calls = []

    async def publish(self, state):
        self.calls.append(state)
        return True


async def allow_runtime(**kwargs):
    return SimpleNamespace(
        allowed=True,
        code="ok",
        entitlement={"revision": 3},
    )


def _identity():
    return VerifiedIdentity(
        uid="synthetic-user",
        email="synthetic@example.test",
        name="Synthetic User",
        timezone="UTC",
    )


def _job():
    return {"id": "00000000-0000-0000-0000-000000000005", "target_schema_version": "hermes-cloud-user-v1"}


def test_cloud_claim_preflights_honcho_and_vendor_before_atomic_publish(monkeypatch):
    monkeypatch.setenv("ELLA_HERMES_CLOUD_PROVISIONING_ENABLED_UIDS", "synthetic-user")
    repository = FakeRepository()
    cloud = FakeCloud()
    honcho = FakeHoncho()
    alert = FakeAlert()
    coordinator = ProvisioningCoordinator(
        repository,
        ForbiddenLocalClient(),
        cloud_client=cloud,
        honcho_client=honcho,
        alert_publisher=alert,
        runtime_admission=allow_runtime,
    )

    asyncio.run(coordinator.process_claimed_job(job=_job(), identity=_identity()))

    assert repository.claims == 1
    assert len(cloud.calls) == 1
    assert len(honcho.calls) == 1
    assert len(repository.finalized) == 1
    assert repository.finalized[0]["status"] == "shadow"
    assert repository.quarantined == []
    assert repository.jobs[-1]["state"] == "ready"
    assert alert.calls[0]["available"] == 1
    assert repository.delivered_alerts == ["alert-a"]


def test_cloud_preflight_failure_quarantines_claim_and_never_publishes(monkeypatch):
    monkeypatch.setenv("ELLA_HERMES_CLOUD_PROVISIONING_ENABLED_UIDS", "synthetic-user")
    repository = FakeRepository()
    coordinator = ProvisioningCoordinator(
        repository,
        ForbiddenLocalClient(),
        cloud_client=FakeCloud(
            error=ProvisioningError("hermes_cloud_tool_drift", retryable=False)
        ),
        honcho_client=FakeHoncho(),
        alert_publisher=FakeAlert(),
        runtime_admission=allow_runtime,
    )

    asyncio.run(coordinator.process_claimed_job(job=_job(), identity=_identity()))

    assert repository.finalized == []
    assert repository.quarantined[0]["reason"] == "hermes_cloud_tool_drift"
    assert repository.jobs[-1]["state"] == "blocked"


def test_empty_pool_emits_durable_low_water_and_stays_retryable(monkeypatch):
    monkeypatch.setenv("ELLA_HERMES_CLOUD_PROVISIONING_ENABLED_UIDS", "synthetic-user")
    repository = FakeRepository(pool_empty=True)
    alert = FakeAlert()
    coordinator = ProvisioningCoordinator(
        repository,
        ForbiddenLocalClient(),
        cloud_client=FakeCloud(),
        honcho_client=FakeHoncho(),
        alert_publisher=alert,
        runtime_admission=allow_runtime,
    )

    asyncio.run(coordinator.process_claimed_job(job=_job(), identity=_identity()))

    assert repository.finalized == []
    assert repository.quarantined == []
    assert repository.jobs[-1]["error_code"] == "runtime_pool_empty"
    assert repository.jobs[-1]["retryable"] is True
    assert alert.calls[0]["available"] == 0
