import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from database.ella_provisioning import (
    EllaProvisioningRepository,
    RuntimePoolClaimError,
)


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

    async def fetchrow(self, query, *args):
        self.queries.append(query)
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
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def fetchrow(self, query, *args):
        self.calls.append((query, args))
        return self.result


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
    assert len(connection.queries) == 3


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
    def __init__(self, *, reconnect=False):
        self.reconnect = reconnect
        self.queries = []
        self.binding_id = uuid.uuid4()
        self.user_id = uuid.uuid4()

    def transaction(self):
        return _AsyncContext(self)

    async def fetchrow(self, query, *args):
        self.queries.append(query)
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
        if "SELECT id FROM users" in query:
            return {"id": self.user_id}
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
        )
    )
    assert reconnect_result["status"] == "claiming"
    assert not any("FOR UPDATE SKIP LOCKED" in query for query in reconnect.queries)


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
    def transaction(self):
        return _AsyncContext(self)

    async def fetchrow(self, query, *args):
        if "b.claim_job_id = $2" in query:
            return {
                "id": uuid.uuid4(),
                "user_id": uuid.uuid4(),
                "role": "user",
                "status": "claiming",
                "claim_lease_expires_at": datetime.now(timezone.utc) - timedelta(seconds=1),
                "omi_uid": args[0],
            }
        raise AssertionError("expired claim must not publish")


def test_cloud_pool_finalize_rechecks_lease_before_publish():
    repository = EllaProvisioningRepository(_Pool(_ExpiredFinalizeConnection()))

    with pytest.raises(RuntimePoolClaimError) as error:
        asyncio.run(
            repository.finalize_cloud_pool_claim(
                uid="synthetic-user",
                job_id=str(uuid.uuid4()),
                claim_token=str(uuid.uuid4()),
                honcho_workspace="workspace-a",
                observed_peer="user-a",
                observer_peer="companion-a",
                health_receipt={"content_free": True},
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
