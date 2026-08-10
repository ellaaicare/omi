import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from database import voice_canary
from database.ella_provisioning import (
    EllaProvisioningRepository,
    RuntimePoolClaimError,
)
from database.runtime_targets import RuntimeTargetLineage

LINEAGE = RuntimeTargetLineage(
    policy_version="ai-data-processors-v8",
    processor_set_hash="sha256:" + ("1" * 64),
    scope_version="managed-cloud-internal-pilot-v2",
    scope_hash="sha256:" + ("2" * 64),
)


def _lock_state_row(query):
    if "pg_backend_pid() AS backend_pid" in query:
        return {
            "backend_pid": 101,
            "transaction_id": 202,
            "lock_held": True,
        }
    return None


class _AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False


class _Connection:
    def __init__(self):
        self.queries = []

    def transaction(self):
        return _AsyncContext(self)

    async def execute(self, query, *_args):
        self.queries.append(query)
        if "pg_advisory_xact_lock" in query:
            return "SELECT 1"
        raise AssertionError(query)

    async def fetch(self, query, *_args):
        self.queries.append(query)
        if "FROM users" in query:
            return []
        raise AssertionError(query)

    async def fetchrow(self, query, *args):
        self.queries.append(query)
        if lock_state := _lock_state_row(query):
            return lock_state
        if "INSERT INTO users" not in query:
            return None

        assert "$5::text" in query
        assert "$2::text" in query
        return {
            "id": args[0],
            "omi_uid": args[4],
            "email": args[1],
            "name": args[2],
            "timezone": args[3],
            "status": "PENDING",
        }


class _Pool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return _AsyncContext(self.connection)


class _LookupPool:
    def __init__(self, result, *, owner_id=None):
        self.result = result
        self.owner_id = owner_id
        self.calls = []

    async def fetchrow(self, query, *args):
        self.calls.append((query, args))
        if lock_state := _lock_state_row(query):
            return lock_state
        if "SELECT id FROM users WHERE omi_uid" in query:
            owner_id = self.owner_id
            if owner_id is None and isinstance(self.result, dict):
                owner_id = (
                    self.result.get("account_user_id")
                    or self.result.get("profile_user_id")
                    or self.result.get("user_id")
                    or self.result.get("id")
                )
            return {"id": owner_id}
        if "SELECT id FROM users WHERE omi_uid" in query and "FOR UPDATE" in query:
            return {"id": self.owner_id}
        return self.result

    async def execute(self, query, *args):
        self.calls.append((query, args))
        return "INSERT 0 1"

    def acquire(self):
        return _AsyncContext(self)

    def transaction(self):
        return _AsyncContext(self)


def test_fresh_identity_insert_casts_jsonb_parameters():
    connection = _Connection()
    repository = EllaProvisioningRepository(_Pool(connection))

    result = asyncio.run(
        repository.ensure_user_identity(
            uid="firebase-user-1",
            email="user@example.com",
            name="Test User",
            timezone_name="America/Los_Angeles",
        )
    )

    assert result == {
        "id": result["id"],
        "omi_uid": "firebase-user-1",
        "email": "user@example.com",
        "name": "Test User",
        "timezone": "America/Los_Angeles",
        "status": "PENDING",
    }
    assert isinstance(result["id"], uuid.UUID)
    assert len(connection.queries) == 6


def test_retained_runtime_requires_active_owned_cluster_with_agent_id():
    pool = _LookupPool({"eligible": True})
    repository = EllaProvisioningRepository(pool)

    assert asyncio.run(repository.has_active_retained_runtime("firebase-user-1")) is True
    query, args = pool.calls[0]
    assert "u.status = 'ACTIVE'" in query
    assert "ac.status = 'ACTIVE'" in query
    assert "ac.agents->>'userAgentId'" in query
    assert args == ("firebase-user-1",)


def test_retained_runtime_rejects_unknown_or_inactive_account():
    repository = EllaProvisioningRepository(_LookupPool({"eligible": False}))

    assert asyncio.run(repository.has_active_retained_runtime("unknown-user")) is False


class _CloudClaimConnection:
    def __init__(
        self,
        *,
        reconnect=False,
        entitlement_revision=3,
        entitlement_status="invited",
        profile_class="synthetic",
    ):
        self.reconnect = reconnect
        self.entitlement_revision = entitlement_revision
        self.entitlement_status = entitlement_status
        self.profile_class = profile_class
        self.queries = []
        self.binding_id = uuid.uuid4()
        self.user_id = uuid.uuid4()

    def transaction(self):
        return _AsyncContext(self)

    async def execute(self, query, *args):
        self.queries.append(query)
        if "pg_advisory_xact_lock" in query:
            return "SELECT 1"
        raise AssertionError(query)

    async def fetch(self, query, *args):
        self.queries.append(query)
        if "FROM voice_kill_switches" in query:
            return []
        raise AssertionError(query)

    async def fetchrow(self, query, *args):
        self.queries.append(query)
        if lock_state := _lock_state_row(query):
            return lock_state
        if "SELECT id FROM users WHERE omi_uid" in query:
            return {"id": self.user_id}
        if "FROM voice_entitlements" in query:
            return {
                "uid": args[0],
                "revision": self.entitlement_revision,
                "status": self.entitlement_status,
                "trial_expires_at": None,
                "provider_allowlist": ["hermes_cloud"],
                "model_allowlist": ["model-a"],
                "daily_limit_s": 2700,
                "monthly_limit_s": 43200,
                "daily_cost_limit_microusd": None,
                "monthly_cost_limit_microusd": None,
                "max_session_s": 1200,
                "max_concurrent": 1,
                "max_audio_bytes_per_session": 120000000,
                "hard_limit_ratio": 1,
                "soft_limit_ratio": 0.8,
            }
        if "FROM voice_usage_events" in query:
            return {
                "daily_used_s": 0,
                "monthly_used_s": 0,
                "daily_cost_microusd": 0,
                "monthly_cost_microusd": 0,
            }
        if "FROM voice_active_sessions" in query:
            return {
                "active_s": 0,
                "active_cost_microusd": 0,
                "active_count": 0,
            }
        if "b.claim_job_id = $2" in query:
            if self.reconnect:
                return {
                    "id": self.binding_id,
                    "user_id": self.user_id,
                    "claim_job_id": args[1],
                    "claim_token": uuid.uuid4(),
                    "status": "claiming",
                    "omi_uid": args[0],
                }
            return None
        if "SELECT id, profile_class FROM users" in query:
            return {"id": self.user_id, "profile_class": self.profile_class}
        if "FOR UPDATE SKIP LOCKED" in query:
            return {"id": self.binding_id}
        if "UPDATE ella_runtime_bindings" in query and "status = 'claiming'" in query:
            return {
                "id": self.binding_id,
                "user_id": self.user_id,
                "claim_job_id": args[2],
                "claim_token": args[3],
                "status": "claiming",
            }
        raise AssertionError(query)


def test_cloud_pool_claim_uses_reconnect_receipt_skip_locked_and_cas():
    connection = _CloudClaimConnection()
    repository = EllaProvisioningRepository(_Pool(connection))
    job_id = str(uuid.uuid4())

    result = asyncio.run(
        repository.claim_cloud_pool_binding(
            uid="synthetic-user",
            job_id=job_id,
            lease_seconds=120,
            admitted_entitlement_revision=3,
            provider="hermes_cloud",
            model="model-a",
            required_profile_class="synthetic",
        )
    )

    joined = "\n".join(connection.queries)
    assert "FOR UPDATE SKIP LOCKED" in joined
    assert "WHERE id = $1" in joined
    assert "AND status = 'pool_available'" in joined
    assert "AND user_id IS NULL" in joined
    assert result["status"] == "claiming"
    assert result["omi_uid"] == "synthetic-user"

    reconnect = _CloudClaimConnection(reconnect=True)
    reconnect_result = asyncio.run(
        EllaProvisioningRepository(_Pool(reconnect)).claim_cloud_pool_binding(
            uid="synthetic-user",
            job_id=job_id,
            lease_seconds=120,
            admitted_entitlement_revision=3,
            provider="hermes_cloud",
            model="model-a",
            required_profile_class="synthetic",
        )
    )
    assert reconnect_result["status"] == "claiming"
    assert not any("FOR UPDATE SKIP LOCKED" in query for query in reconnect.queries)


@pytest.mark.parametrize(
    ("connection", "expected_code"),
    [
        (_CloudClaimConnection(entitlement_revision=4), "runtime_admission_entitlement_stale"),
        (_CloudClaimConnection(entitlement_status="revoked"), "runtime_admission_revoked"),
    ],
)
def test_cloud_pool_claim_revalidates_entitlement_before_selecting_candidate(
    connection,
    expected_code,
):
    repository = EllaProvisioningRepository(_Pool(connection))

    with pytest.raises(RuntimePoolClaimError) as error:
        asyncio.run(
            repository.claim_cloud_pool_binding(
                uid="synthetic-user",
                job_id=str(uuid.uuid4()),
                lease_seconds=120,
                admitted_entitlement_revision=3,
                provider="hermes_cloud",
                model="model-a",
                required_profile_class="synthetic",
            )
        )

    assert error.value.code == expected_code
    assert not any("FOR UPDATE SKIP LOCKED" in query for query in connection.queries)


def test_cloud_pool_claim_rejects_real_profile_even_when_selected():
    connection = _CloudClaimConnection(profile_class="real")
    repository = EllaProvisioningRepository(_Pool(connection))

    with pytest.raises(RuntimePoolClaimError) as error:
        asyncio.run(
            repository.claim_cloud_pool_binding(
                uid="allowlisted-real-user",
                job_id=str(uuid.uuid4()),
                lease_seconds=120,
                admitted_entitlement_revision=3,
                provider="hermes_cloud",
                model="model-a",
                required_profile_class="synthetic",
            )
        )

    assert error.value.code == "hermes_cloud_synthetic_profile_required"
    assert not any("FOR UPDATE SKIP LOCKED" in query for query in connection.queries)


def test_cloud_pool_registration_persists_prompt_artifact_receipt():
    artifact_receipt = {
        "prompt_pack_version": "prompt-v1",
        "model_policy_version": "model-policy-v1",
        "soul_sha256": "a" * 64,
        "agents_sha256": "b" * 64,
        "model_policy_sha256": "c" * 64,
        "content_free": True,
    }
    pool = _LookupPool(
        {
            "id": uuid.uuid4(),
            "runtime_instance_id": "instance-a",
            "status": "pool_available",
        }
    )
    repository = EllaProvisioningRepository(pool)

    result = asyncio.run(
        repository.register_cloud_pool_binding(
            runtime_instance_id="instance-a",
            profile_name="pool-instance-a",
            agent_id="hermes-cloud",
            api_base_url_ref="env:ELLA_HERMES_CLOUD_API_URL_POOL_01",
            api_key_ref="env:ELLA_HERMES_CLOUD_API_KEY_POOL_01",
            honcho_api_key_ref="env:ELLA_HONCHO_CLOUD_API_KEY",
            template_version="hermes-cloud-user-v1",
            prompt_pack_version="prompt-v1",
            prompt_artifact_receipt=artifact_receipt,
            model_policy_version="model-policy-v1",
            voice_policy_version="voice-v1",
            expected_model="model-a",
            allowed_tools=[],
            required_capabilities=["responses_api", "session_key_header"],
            health_receipt={"content_free": True},
        )
    )

    query, args = pool.calls[0]
    assert result["status"] == "pool_available"
    assert "prompt_artifact_receipt" in query
    assert len(args) == 16
    assert '"prompt_pack_version": "prompt-v1"' in args[9]


class _ExpiredFinalizeConnection:
    def __init__(self):
        self.user_id = uuid.uuid4()

    def transaction(self):
        return _AsyncContext(self)

    async def execute(self, query, *_args):
        if "pg_advisory_xact_lock" in query:
            return "SELECT 1"
        raise AssertionError(query)

    async def fetchrow(self, query, *args):
        if lock_state := _lock_state_row(query):
            return lock_state
        if "SELECT id FROM users WHERE omi_uid" in query:
            return {"id": self.user_id}
        if "b.claim_job_id = $2" in query:
            return {
                "id": uuid.uuid4(),
                "user_id": self.user_id,
                "role": "user",
                "status": "claiming",
                "api_base_url_ref": "env:ELLA_HERMES_CLOUD_API_URL_SYNTHETIC",
                "api_key_ref": "env:ELLA_HERMES_CLOUD_API_KEY_SYNTHETIC",
                "honcho_api_key_ref": None,
                "expected_model": "gpt-5.6-terra",
                "claim_lease_expires_at": datetime.now(timezone.utc) - timedelta(seconds=1),
                "omi_uid": args[0],
            }
        raise AssertionError("expired claim must not publish")


def test_cloud_pool_finalize_rechecks_lease_before_publish(monkeypatch):
    repository = EllaProvisioningRepository(_Pool(_ExpiredFinalizeConnection()))

    async def allow_revalidation(*_args, **_kwargs):
        return voice_canary.VoicePolicyDecision(
            allowed=True,
            code="ok",
            entitlement={
                "consent_policy_version": LINEAGE.policy_version,
                "consent_processor_set_hash": LINEAGE.processor_set_hash,
                "consent_scope_version": LINEAGE.scope_version,
                "consent_scope_hash": LINEAGE.scope_hash,
            },
            quota={},
        )

    monkeypatch.setattr(
        voice_canary,
        "revalidate_runtime_activation_on_connection",
        allow_revalidation,
    )
    with pytest.raises(RuntimePoolClaimError) as error:
        asyncio.run(
            repository.finalize_cloud_pool_claim(
                uid="synthetic-user",
                job_id=str(uuid.uuid4()),
                claim_token=str(uuid.uuid4()),
                honcho_workspace="workspace-a",
                observed_peer="user-a",
                observer_peer="companion-a",
                admitted_entitlement_revision=3,
                authority_lineage=LINEAGE,
                health_receipt={
                    "content_free": True,
                    **LINEAGE.as_dict(),
                    "admission_revision": 3,
                },
            )
        )

    assert str(error.value) == "runtime_pool_claim_expired"


class _IngestionPool:
    def __init__(self):
        self.calls = []
        self.inserted = False

    async def fetchrow(self, query, *args):
        self.calls.append((query, args))
        if "INSERT INTO ella_runtime_ingestion_receipts" in query:
            if self.inserted:
                return None
            self.inserted = True
            return {"id": uuid.uuid4(), "status": "claimed"}
        if "SELECT *" in query:
            return {"id": uuid.uuid4(), "status": "claimed"}
        raise AssertionError(query)


def test_runtime_ingestion_identity_includes_event_revision():
    pool = _IngestionPool()
    repository = EllaProvisioningRepository(pool)
    binding_id = str(uuid.uuid4())

    first = asyncio.run(
        repository.claim_runtime_ingestion(
            binding_id=binding_id,
            canonical_event_id="event-a",
            source_identity="source-a",
            event_revision=1,
            provenance="canonical",
        )
    )
    second = asyncio.run(
        repository.claim_runtime_ingestion(
            binding_id=binding_id,
            canonical_event_id="event-a",
            source_identity="source-a",
            event_revision=1,
            provenance="canonical",
        )
    )

    assert first["inserted"] is True
    assert second["inserted"] is False
    insert_query = pool.calls[0][0]
    assert "event_revision" in insert_query
    assert "ON CONFLICT" in insert_query


class _InteractionClaimConnection:
    def __init__(self, *, running_other=False):
        self.running_other = running_other
        self.scope_id = uuid.uuid4()
        self.queries = []

    def transaction(self):
        return _AsyncContext(self)

    async def fetchrow(self, query, *args):
        self.queries.append((query, args))
        if "SELECT i.*" in query:
            return {
                "id": args[0],
                "scope_id": self.scope_id,
                "status": "pending",
            }
        if "status = 'completed'" in query:
            return {
                "provider_response_id": "response-previous",
                "usage": json.dumps({"input_tokens": 100, "output_tokens": 25}),
            }
        if "UPDATE ella_runtime_interactions" in query:
            return {
                "id": args[0],
                "scope_id": self.scope_id,
                "status": "running",
                "previous_response_id": args[1],
            }
        raise AssertionError(query)

    async def fetchval(self, query, *args):
        self.queries.append((query, args))
        if "status = 'running'" in query:
            return 1 if self.running_other else None
        raise AssertionError(query)


def test_runtime_interaction_claim_serializes_scope_and_assigns_predecessor_at_claim():
    interaction_id = str(uuid.uuid4())
    connection = _InteractionClaimConnection()
    result = asyncio.run(EllaProvisioningRepository(_Pool(connection)).claim_runtime_interaction(interaction_id))

    assert result["status"] == "running"
    assert result["previous_response_id"] == "response-previous"
    assert result["previous_response_usage"] == {
        "input_tokens": 100,
        "output_tokens": 25,
    }
    joined = "\n".join(query for query, _args in connection.queries)
    assert "FOR UPDATE OF s, i" in joined
    assert "ORDER BY completed_at DESC, created_at DESC, id DESC" in joined

    blocked_connection = _InteractionClaimConnection(running_other=True)
    blocked = asyncio.run(
        EllaProvisioningRepository(_Pool(blocked_connection)).claim_runtime_interaction(str(uuid.uuid4()))
    )
    assert blocked is None
    assert not any("UPDATE ella_runtime_interactions" in query for query, _args in blocked_connection.queries)


def test_stateless_runtime_interaction_skips_predecessor_at_create_and_claim():
    class CreatePool:
        def __init__(self):
            self.calls = []

        async def fetchval(self, query, *args):
            raise AssertionError("stateless creation must not query a predecessor")

        async def fetchrow(self, query, *args):
            self.calls.append((query, args))
            return {
                "id": args[0],
                "request_hash": args[3],
                "previous_response_id": args[7],
            }

    create_pool = CreatePool()
    repository = EllaProvisioningRepository(create_pool)
    created = asyncio.run(
        repository.get_or_create_runtime_interaction(
            scope_id=str(uuid.uuid4()),
            client_interaction_id="stateless-verifier",
            request_hash="request-hash",
            correlation_id="correlation-a",
            canonical_user_event_id="user-event-a",
            canonical_assistant_event_id="assistant-event-a",
            allow_previous_response=False,
        )
    )

    assert created["previous_response_id"] is None
    assert create_pool.calls[0][1][7] is None

    interaction_id = str(uuid.uuid4())
    claim_connection = _InteractionClaimConnection()
    claimed = asyncio.run(
        EllaProvisioningRepository(_Pool(claim_connection)).claim_runtime_interaction(
            interaction_id,
            allow_previous_response=False,
        )
    )

    assert claimed["previous_response_id"] is None
    assert claimed["previous_response_usage"] == {}
    joined = "\n".join(query for query, _args in claim_connection.queries)
    assert "ORDER BY completed_at DESC, created_at DESC, id DESC" not in joined


def test_shadow_promotion_is_explicit_owner_scoped_revision_cas(monkeypatch):
    owner_id = uuid.uuid4()
    pool = _LookupPool(
        {
            "id": uuid.uuid4(),
            "status": "internal_canary",
            "active": True,
            "revision": 3,
            "account_user_id": owner_id,
            "profile_user_id": owner_id,
            "role": "user",
            "runtime_target_mode": "hermes-cloud-chat",
            "runtime_instance_id": "instance-a",
            "target_endpoint_ref": "env:ELLA_HERMES_CLOUD_API_URL_SYNTHETIC",
            "target_credential_ref": "env:ELLA_HERMES_CLOUD_API_KEY_SYNTHETIC",
            "expected_model": "gpt-5.6-terra",
            "health_receipt": {
                "policy_version": "ai-data-processors-v8",
                "processor_set_hash": "sha256:" + ("1" * 64),
                "scope_version": "managed-cloud-internal-pilot-v2",
                "scope_hash": "sha256:" + ("2" * 64),
                "admission_revision": 3,
            },
        },
        owner_id=owner_id,
    )
    binding_id = str(uuid.uuid4())

    async def allow_revalidation(*_args, **_kwargs):
        return voice_canary.VoicePolicyDecision(
            allowed=True,
            code="ok",
            entitlement={
                "consent_policy_version": LINEAGE.policy_version,
                "consent_processor_set_hash": LINEAGE.processor_set_hash,
                "consent_scope_version": LINEAGE.scope_version,
                "consent_scope_hash": LINEAGE.scope_hash,
            },
            quota={},
        )

    monkeypatch.setattr(
        voice_canary,
        "revalidate_runtime_activation_on_connection",
        allow_revalidation,
    )
    result = asyncio.run(
        EllaProvisioningRepository(pool).promote_cloud_binding(
            uid="synthetic-user",
            binding_id=binding_id,
            expected_revision=2,
            target_status="internal_canary",
            required_profile_class="synthetic",
            admitted_entitlement_revision=3,
            authority_lineage=LINEAGE,
        )
    )

    query, args = next((query, args) for query, args in pool.calls if "UPDATE ella_runtime_bindings b" in query)
    assert result["status"] == "internal_canary"
    assert "u.omi_uid = $2" in query
    assert "b.status = 'shadow'" in query
    assert "b.active = false" in query
    assert "b.revision = $3" in query
    assert "u.profile_class = $5" in query
    assert args == (
        uuid.UUID(binding_id),
        "synthetic-user",
        2,
        "internal_canary",
        "synthetic",
    )
