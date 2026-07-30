import asyncio
import json
from types import SimpleNamespace

import pytest

from database.runtime_targets import RuntimeTargetLineage
from ella.services import provisioning
from ella.services.hermes_cloud import HermesCloudClient, HermesCloudPreflight
from ella.services.hermes_cloud_policy import CurrentCloudAuthority
from ella.services.provisioning import (
    HermesProvisionClient,
    ProvisioningCoordinator,
    ProvisioningError,
    VerifiedIdentity,
)

LINEAGE = RuntimeTargetLineage(
    policy_version="ai-data-processors-v8",
    processor_set_hash="sha256:" + ("1" * 64),
    scope_version="managed-cloud-internal-pilot-v2",
    scope_hash="sha256:" + ("2" * 64),
)


@pytest.fixture(autouse=True)
def current_cloud_lineage(monkeypatch):
    def current_authority(uid, **kwargs):
        if kwargs.get("profile_class") != "synthetic":
            raise ProvisioningError("hermes_cloud_synthetic_profile_required", retryable=False)
        return CurrentCloudAuthority(
            consent_receipt_id=f"receipt-{uid}",
            profile_binding_id=f"profile-{uid}",
            lineage=LINEAGE,
        )

    monkeypatch.setattr(provisioning, "current_cloud_authority", current_authority)


class ForbiddenLocalClient(HermesProvisionClient):
    async def provision(self, identity, target_schema_version):
        raise AssertionError("cloud provisioning must not call the Mini provision shim")


class FakeRepository:
    def __init__(self, *, pool_empty=False, claim_error=None, ready_receipt_error=None):
        self.pool_empty = pool_empty
        self.claim_error = claim_error
        self.ready_receipt_error = ready_receipt_error
        self.quarantined = []
        self.finalized = []
        self.jobs = []
        self.claims = 0
        self.delivered_alerts = []
        self.side_effects = []
        self.rollbacks = []
        self.call_order = []
        self.claim_arguments = []
        self.profile_class = "synthetic"

    async def get_cloud_profile_class(self, uid):
        assert uid == "synthetic-user"
        return self.profile_class

    async def get_cloud_pool_admission_policy(self):
        self.call_order.append("admission_policy")
        if self.pool_empty:
            return None
        return {"provider": "hermes_cloud", "model": "model-a"}

    async def claim_cloud_pool_binding(self, **kwargs):
        self.call_order.append("pool_claim")
        self.claim_arguments.append(dict(kwargs))
        self.claims += 1
        if self.claim_error:
            raise self.claim_error
        if self.pool_empty:
            return None
        return {
            "id": "binding-a",
            "claim_token": "00000000-0000-0000-0000-000000000004",
            "expected_model": "model-a",
            "api_base_url_ref": "env:ELLA_HERMES_CLOUD_API_URL_SYNTHETIC",
            "api_key_ref": "env:ELLA_HERMES_CLOUD_API_KEY_SYNTHETIC",
            "honcho_api_key_ref": "env:ELLA_HONCHO_CLOUD_API_KEY_SYNTHETIC",
            "profile_class": self.profile_class,
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
        return {"status": "quarantined"}

    async def update_job(self, **kwargs):
        if kwargs.get("state") == "ready" and self.ready_receipt_error:
            raise self.ready_receipt_error
        self.jobs.append(kwargs)
        return kwargs

    async def record_cloud_side_effect(self, **kwargs):
        self.side_effects.append(kwargs["effect"])

    async def get_cloud_side_effects(self, job_id):
        return list(self.side_effects)

    async def record_cloud_rollback(self, **kwargs):
        self.rollbacks.append(kwargs)
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
    def __init__(self, *, cleanup_error=None):
        self.calls = []
        self.cleanup_calls = []
        self.cleanup_error = cleanup_error

    async def ensure_profile(self, binding, *, on_side_effect):
        self.calls.append(binding)
        await on_side_effect(
            {
                "kind": "honcho_workspace",
                "workspace": "workspace-a",
            }
        )
        await on_side_effect(
            {
                "kind": "honcho_peer",
                "workspace": "workspace-a",
                "peer": "user-a",
            }
        )
        return {
            "workspace": "workspace-a",
            "observed_peer": "user-a",
            "observer_peer": "companion-a",
        }

    async def cleanup_profile(self, binding, side_effects):
        self.cleanup_calls.append((binding, side_effects))
        if self.cleanup_error:
            raise self.cleanup_error
        return {"status": "cleaned", "content_free": True}


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
        entitlement={"revision": 3, "status": "invited"},
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


def test_cloud_claim_preflights_vendor_without_honcho_before_atomic_publish(monkeypatch):
    monkeypatch.setenv("ELLA_HERMES_CLOUD_PROVISIONING_ENABLED_UIDS", "synthetic-user")
    monkeypatch.setenv("ELLA_HERMES_CLOUD_SYNTHETIC_UIDS", "synthetic-user")
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
    assert repository.call_order == ["admission_policy", "pool_claim"]
    assert repository.claim_arguments == [
        {
            "uid": "synthetic-user",
            "job_id": "00000000-0000-0000-0000-000000000005",
            "lease_seconds": 120,
            "admitted_entitlement_revision": 3,
            "provider": "hermes_cloud",
            "model": "model-a",
            "required_profile_class": "synthetic",
        }
    ]
    assert len(cloud.calls) == 1
    assert len(honcho.calls) == 0
    assert len(repository.finalized) == 1
    assert repository.finalized[0]["status"] == "shadow"
    assert repository.finalized[0]["health_receipt"]["admission_revision"] == 3
    assert repository.finalized[0]["health_receipt"]["memory"]["provider"] == "hermes_profile_scoped_memory"
    assert repository.quarantined == []
    assert repository.jobs[-1]["state"] == "ready"
    assert alert.calls[0]["available"] == 1
    assert repository.delivered_alerts == ["alert-a"]


def test_nonstaged_finalization_accepts_real_postgres_jsonb_projection(monkeypatch):
    monkeypatch.setenv("ELLA_HERMES_CLOUD_PROVISIONING_ENABLED_UIDS", "synthetic-user")
    monkeypatch.setenv("ELLA_HERMES_CLOUD_SYNTHETIC_UIDS", "synthetic-user")
    monkeypatch.setenv("ELLA_HERMES_CLOUD_API_URL_SYNTHETIC", "https://cloud.example.test")
    monkeypatch.setenv("ELLA_HERMES_CLOUD_API_KEY_SYNTHETIC", "synthetic-test-token")
    artifact_hash = "a" * 64

    class AsyncpgProjectionRepository(FakeRepository):
        async def claim_cloud_pool_binding(self, **kwargs):
            binding = await super().claim_cloud_pool_binding(**kwargs)
            binding.update(
                {
                    "prompt_pack_version": "prompt-v1",
                    "model_policy_version": "models-v1",
                    "allowed_tools": json.dumps([]),
                    "required_capabilities": json.dumps(["responses_api", "session_key_header"]),
                    "prompt_artifact_receipt": json.dumps(
                        {
                            "schema_version": "ella-hermes-cloud-approval-v1",
                            "prompt_pack_version": "prompt-v1",
                            "model_policy_version": "models-v1",
                            "expected_model": "model-a",
                            "model_context_window_tokens": 16384,
                            "policy_commit_sha": "b" * 40,
                            "lane_s_review_url": "https://github.com/ellaaicare/ella-ai/issues/1124",
                            "approval_manifest_sha256": "c" * 64,
                            "soul_sha256": artifact_hash,
                            "observed_soul_sha256": artifact_hash,
                            "agents_sha256": artifact_hash,
                            "observed_agents_sha256": artifact_hash,
                            "model_policy_sha256": artifact_hash,
                            "observed_model_policy_sha256": artifact_hash,
                        },
                        sort_keys=True,
                    ),
                }
            )
            return binding

    class Response:
        def __init__(self, body):
            self.status_code = 200
            self._body = body

        def json(self):
            return self._body

    class PreflightHttpClient:
        def __init__(self):
            self.calls = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            bodies = {
                "https://cloud.example.test/health/detailed": {
                    "status": "ok",
                    "readiness": {"status": "ok"},
                },
                "https://cloud.example.test/v1/capabilities": {
                    "session_key_header": "X-Hermes-Session-Key",
                    "features": {"responses_api": True},
                },
                "https://cloud.example.test/v1/models": {"data": [{"id": "model-a"}]},
                "https://cloud.example.test/v1/toolsets": [],
            }
            return Response(bodies[url])

    repository = AsyncpgProjectionRepository()
    http_client = PreflightHttpClient()
    coordinator = ProvisioningCoordinator(
        repository,
        ForbiddenLocalClient(),
        cloud_client=HermesCloudClient(http_client_factory=lambda **_kwargs: http_client),
        honcho_client=FakeHoncho(),
        alert_publisher=FakeAlert(),
        runtime_admission=allow_runtime,
    )

    asyncio.run(coordinator.process_claimed_job(job=_job(), identity=_identity()))

    assert len(http_client.calls) == 4
    assert len(repository.finalized) == 1
    assert repository.jobs[-1]["state"] == "ready"
    assert repository.quarantined == []


def test_cloud_claim_finalization_revalidates_staged_receipt_without_direct_preflight(monkeypatch):
    monkeypatch.setenv("ELLA_HERMES_CLOUD_PROVISIONING_ENABLED_UIDS", "synthetic-user")
    monkeypatch.setenv("ELLA_HERMES_CLOUD_SYNTHETIC_UIDS", "synthetic-user")
    staged_marker = {
        "attestation_id": "attestation-canary",
        "receipt_ref": "/var/lib/ella/hermes-cloud-attestations/canary.json",
        "receipt_sha256": "d" * 64,
        "phase": "pool_registration",
    }

    class StagedRepository(FakeRepository):
        async def claim_cloud_pool_binding(self, **kwargs):
            binding = await super().claim_cloud_pool_binding(**kwargs)
            binding.update(
                {
                    "user_id": "11111111-1111-4111-8111-111111111111",
                    "runtime_instance_id": "instance-a",
                    "health_receipt": {"staged_attestation": staged_marker},
                }
            )
            return binding

    class NoDirectCloud:
        async def preflight(self, binding):
            raise AssertionError("direct Nous preflight must not run for staged finalization")

    class Verifier:
        def preflight(self, binding, **kwargs):
            assert kwargs["phase"] == "claim_finalization"
            assert kwargs["prior_marker"] == staged_marker
            return {
                "model": "model-a",
                "tools": [],
                "capabilities": ["responses_api", "session_key_header"],
                "preflight_source": "server_staged_attestation",
                "staged_attestation": {**staged_marker, "phase": "claim_finalization"},
                "content_free": True,
            }

    repository = StagedRepository()
    coordinator = ProvisioningCoordinator(
        repository,
        ForbiddenLocalClient(),
        cloud_client=NoDirectCloud(),
        honcho_client=FakeHoncho(),
        alert_publisher=FakeAlert(),
        runtime_admission=allow_runtime,
        staged_attestation_verifier=Verifier(),
    )

    asyncio.run(coordinator.process_claimed_job(job=_job(), identity=_identity()))

    assert len(repository.finalized) == 1
    health = repository.finalized[0]["health_receipt"]
    assert health["preflight_source"] == "server_staged_attestation"
    assert health["staged_attestation"]["phase"] == "claim_finalization"


def test_allowlisted_real_profile_is_denied_before_claim_or_vendor_side_effect(
    monkeypatch,
):
    monkeypatch.setenv("ELLA_HERMES_CLOUD_PROVISIONING_ENABLED_UIDS", "synthetic-user")
    monkeypatch.setenv("ELLA_HERMES_CLOUD_SYNTHETIC_UIDS", "synthetic-user")
    repository = FakeRepository()
    repository.profile_class = "real"
    cloud = FakeCloud()
    honcho = FakeHoncho()
    coordinator = ProvisioningCoordinator(
        repository,
        ForbiddenLocalClient(),
        cloud_client=cloud,
        honcho_client=honcho,
        alert_publisher=FakeAlert(),
        runtime_admission=allow_runtime,
    )

    asyncio.run(coordinator.process_claimed_job(job=_job(), identity=_identity()))

    assert repository.claims == 0
    assert cloud.calls == []
    assert honcho.calls == []
    assert repository.jobs[-1]["error_code"] == "hermes_cloud_synthetic_profile_required"


def test_cloud_preflight_failure_quarantines_claim_and_never_publishes(monkeypatch):
    monkeypatch.setenv("ELLA_HERMES_CLOUD_PROVISIONING_ENABLED_UIDS", "synthetic-user")
    monkeypatch.setenv("ELLA_HERMES_CLOUD_SYNTHETIC_UIDS", "synthetic-user")
    repository = FakeRepository()
    coordinator = ProvisioningCoordinator(
        repository,
        ForbiddenLocalClient(),
        cloud_client=FakeCloud(error=ProvisioningError("hermes_cloud_tool_drift", retryable=False)),
        honcho_client=FakeHoncho(),
        alert_publisher=FakeAlert(),
        runtime_admission=allow_runtime,
    )

    asyncio.run(coordinator.process_claimed_job(job=_job(), identity=_identity()))

    assert repository.finalized == []
    assert repository.quarantined[0]["reason"] == "hermes_cloud_tool_drift"
    assert repository.jobs[-1]["state"] == "blocked"
    assert repository.rollbacks[-1]["rollback_receipt"]["status"] == "not_required"


def test_post_publication_ready_receipt_failure_still_quarantines_claim(monkeypatch):
    monkeypatch.setenv("ELLA_HERMES_CLOUD_PROVISIONING_ENABLED_UIDS", "synthetic-user")
    monkeypatch.setenv("ELLA_HERMES_CLOUD_SYNTHETIC_UIDS", "synthetic-user")
    repository = FakeRepository(ready_receipt_error=RuntimeError("synthetic receipt failure"))
    coordinator = ProvisioningCoordinator(
        repository,
        ForbiddenLocalClient(),
        cloud_client=FakeCloud(),
        honcho_client=FakeHoncho(),
        alert_publisher=FakeAlert(),
        runtime_admission=allow_runtime,
    )

    asyncio.run(coordinator.process_claimed_job(job=_job(), identity=_identity()))

    assert len(repository.finalized) == 1
    assert repository.quarantined[0]["reason"] == "cloud_provisioning_internal_error"
    assert repository.rollbacks[-1]["state"] == "blocked"
    assert repository.rollbacks[-1]["rollback_receipt"]["quarantined"] is True


def test_consent_revoked_after_claim_blocks_honcho_and_cloud_side_effects(monkeypatch):
    monkeypatch.setenv("ELLA_HERMES_CLOUD_PROVISIONING_ENABLED_UIDS", "synthetic-user")
    repository = FakeRepository()
    cloud = FakeCloud()
    honcho = FakeHoncho()
    checks = 0

    def consent_gate(uid, **_kwargs):
        nonlocal checks
        checks += 1
        if checks == 2:
            raise ProvisioningError("managed_cloud_consent_required", retryable=False)
        return CurrentCloudAuthority(
            consent_receipt_id=f"receipt-{uid}",
            profile_binding_id=f"profile-{uid}",
            lineage=LINEAGE,
        )

    monkeypatch.setattr(provisioning, "current_cloud_authority", consent_gate)
    coordinator = ProvisioningCoordinator(
        repository,
        ForbiddenLocalClient(),
        cloud_client=cloud,
        honcho_client=honcho,
        alert_publisher=FakeAlert(),
        runtime_admission=allow_runtime,
    )

    asyncio.run(coordinator.process_claimed_job(job=_job(), identity=_identity()))

    assert checks == 2
    assert repository.claims == 1
    assert honcho.calls == []
    assert cloud.calls == []
    assert repository.finalized == []
    assert repository.quarantined[0]["reason"] == "managed_cloud_consent_required"


def test_empty_pool_emits_durable_low_water_and_stays_retryable(monkeypatch):
    monkeypatch.setenv("ELLA_HERMES_CLOUD_PROVISIONING_ENABLED_UIDS", "synthetic-user")
    monkeypatch.setenv("ELLA_HERMES_CLOUD_SYNTHETIC_UIDS", "synthetic-user")
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
    assert repository.claims == 0


def test_entitlement_denial_happens_before_pool_claim_or_vendor_side_effect(monkeypatch):
    monkeypatch.setenv("ELLA_HERMES_CLOUD_PROVISIONING_ENABLED_UIDS", "synthetic-user")
    monkeypatch.setenv("ELLA_HERMES_CLOUD_SYNTHETIC_UIDS", "synthetic-user")
    repository = FakeRepository()
    cloud = FakeCloud()
    honcho = FakeHoncho()

    async def deny_runtime(**kwargs):
        repository.call_order.append("entitlement_denied")
        return SimpleNamespace(allowed=False, code="no_entitlement", entitlement=None)

    coordinator = ProvisioningCoordinator(
        repository,
        ForbiddenLocalClient(),
        cloud_client=cloud,
        honcho_client=honcho,
        alert_publisher=FakeAlert(),
        runtime_admission=deny_runtime,
    )

    asyncio.run(coordinator.process_claimed_job(job=_job(), identity=_identity()))

    assert repository.call_order == ["admission_policy", "entitlement_denied"]
    assert repository.claims == 0
    assert cloud.calls == []
    assert honcho.calls == []
    assert repository.jobs[-1]["error_code"] == "runtime_admission_no_entitlement"


def test_atomic_claim_revalidation_denial_has_zero_honcho_or_cloud_side_effects(
    monkeypatch,
):
    monkeypatch.setenv("ELLA_HERMES_CLOUD_PROVISIONING_ENABLED_UIDS", "synthetic-user")
    monkeypatch.setenv("ELLA_HERMES_CLOUD_SYNTHETIC_UIDS", "synthetic-user")
    repository = FakeRepository(claim_error=provisioning.RuntimePoolClaimError("runtime_admission_revoked"))
    cloud = FakeCloud()
    honcho = FakeHoncho()
    coordinator = ProvisioningCoordinator(
        repository,
        ForbiddenLocalClient(),
        cloud_client=cloud,
        honcho_client=honcho,
        alert_publisher=FakeAlert(),
        runtime_admission=allow_runtime,
    )

    asyncio.run(coordinator.process_claimed_job(job=_job(), identity=_identity()))

    assert repository.claims == 1
    assert repository.side_effects == []
    assert repository.quarantined == []
    assert honcho.calls == []
    assert cloud.calls == []
    assert repository.jobs[-1]["state"] == "blocked"
    assert repository.jobs[-1]["error_code"] == "runtime_admission_revoked"


def test_authority_change_after_claim_blocks_honcho_and_quarantines_claim(
    monkeypatch,
):
    monkeypatch.setenv("ELLA_HERMES_CLOUD_PROVISIONING_ENABLED_UIDS", "synthetic-user")
    monkeypatch.setenv("ELLA_HERMES_CLOUD_SYNTHETIC_UIDS", "synthetic-user")
    repository = FakeRepository()
    cloud = FakeCloud()
    honcho = FakeHoncho()
    admissions = 0

    async def change_authority_after_claim(**_kwargs):
        nonlocal admissions
        admissions += 1
        if admissions == 1:
            return SimpleNamespace(
                allowed=True,
                code="ok",
                entitlement={"revision": 3, "status": "invited"},
            )
        return SimpleNamespace(
            allowed=False,
            code="provider_disabled",
            entitlement={"revision": 3, "status": "invited"},
        )

    coordinator = ProvisioningCoordinator(
        repository,
        ForbiddenLocalClient(),
        cloud_client=cloud,
        honcho_client=honcho,
        alert_publisher=FakeAlert(),
        runtime_admission=change_authority_after_claim,
    )

    asyncio.run(coordinator.process_claimed_job(job=_job(), identity=_identity()))

    assert admissions == 2
    assert repository.claims == 1
    assert repository.side_effects == []
    assert honcho.calls == []
    assert cloud.calls == []
    assert repository.finalized == []
    assert repository.quarantined[0]["reason"] == "runtime_admission_provider_disabled"


def test_cloud_preflight_retryable_failure_has_no_honcho_side_effects_and_quarantines(monkeypatch):
    monkeypatch.setenv("ELLA_HERMES_CLOUD_PROVISIONING_ENABLED_UIDS", "synthetic-user")
    monkeypatch.setenv("ELLA_HERMES_CLOUD_SYNTHETIC_UIDS", "synthetic-user")
    repository = FakeRepository()
    honcho = FakeHoncho()
    coordinator = ProvisioningCoordinator(
        repository,
        ForbiddenLocalClient(),
        cloud_client=FakeCloud(error=ProvisioningError("hermes_cloud_tool_drift", retryable=True)),
        honcho_client=honcho,
        alert_publisher=FakeAlert(),
        runtime_admission=allow_runtime,
    )

    asyncio.run(coordinator.process_claimed_job(job=_job(), identity=_identity()))

    assert repository.side_effects == []
    assert honcho.cleanup_calls == []
    assert len(repository.quarantined) == 1
    assert repository.rollbacks[-1]["state"] == "retryable"


def test_honcho_cleanup_failure_is_irrelevant_without_cloud_honcho_side_effects(monkeypatch):
    monkeypatch.setenv("ELLA_HERMES_CLOUD_PROVISIONING_ENABLED_UIDS", "synthetic-user")
    monkeypatch.setenv("ELLA_HERMES_CLOUD_SYNTHETIC_UIDS", "synthetic-user")
    repository = FakeRepository()
    coordinator = ProvisioningCoordinator(
        repository,
        ForbiddenLocalClient(),
        cloud_client=FakeCloud(error=ProvisioningError("hermes_cloud_tool_drift", retryable=True)),
        honcho_client=FakeHoncho(cleanup_error=RuntimeError("cleanup failed")),
        alert_publisher=FakeAlert(),
        runtime_admission=allow_runtime,
    )

    asyncio.run(coordinator.process_claimed_job(job=_job(), identity=_identity()))

    assert repository.rollbacks[-1]["state"] == "retryable"
    assert repository.rollbacks[-1]["retryable"] is True
    assert repository.rollbacks[-1]["rollback_receipt"]["status"] == "not_required"
