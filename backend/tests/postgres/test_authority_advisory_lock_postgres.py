import asyncio
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import asyncpg
import pytest

from database import authority_advisory_lock, content_write_fence, managed_cloud_consent, voice_canary
from database.ella_provisioning import EllaProvisioningRepository
from database.runtime_targets import RuntimeTargetLineage

TEST_DSN = os.getenv("ELLA_TEST_POSTGRES_DSN", "").strip()
MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
LINEAGE = RuntimeTargetLineage(
    policy_version="ai-data-processors-v8",
    processor_set_hash="sha256:" + ("1" * 64),
    scope_version="managed-cloud-internal-pilot-v2",
    scope_hash="sha256:" + ("2" * 64),
)

pytestmark = pytest.mark.skipif(
    not TEST_DSN,
    reason="ELLA_TEST_POSTGRES_DSN is required for authority-lock PostgreSQL tests",
)

BASE_PROVISIONING_SCHEMA = """
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    omi_uid TEXT UNIQUE,
    email TEXT UNIQUE,
    name TEXT NOT NULL DEFAULT 'Synthetic User',
    timezone TEXT NOT NULL DEFAULT 'UTC',
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    identities JSONB NOT NULL DEFAULT '{}'::jsonb,
    settings JSONB NOT NULL DEFAULT '{}'::jsonb,
    tags TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE ella_provisioning_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    target_schema_version TEXT NOT NULL,
    client_request_id TEXT,
    request_payload_hash TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'pending',
    stage TEXT NOT NULL DEFAULT 'identity_ready',
    retryable BOOLEAN NOT NULL DEFAULT true,
    error_code TEXT,
    error_detail JSONB NOT NULL DEFAULT '{}'::jsonb,
    attempts INTEGER NOT NULL DEFAULT 0,
    receipts JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE ella_runtime_bindings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    provider TEXT NOT NULL,
    profile_name TEXT UNIQUE,
    agent_id TEXT NOT NULL,
    workspace_root TEXT,
    internal_gateway_url TEXT,
    gateway_port INTEGER UNIQUE,
    service_label TEXT UNIQUE,
    credential_ref TEXT,
    honcho_workspace TEXT UNIQUE,
    observed_peer TEXT UNIQUE,
    observer_peer TEXT UNIQUE,
    template_version TEXT NOT NULL,
    model_policy_version TEXT NOT NULL,
    voice_policy_version TEXT NOT NULL,
    health_state TEXT NOT NULL DEFAULT 'pending',
    health_receipt JSONB NOT NULL DEFAULT '{}'::jsonb,
    revision INTEGER NOT NULL DEFAULT 1,
    active BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ella_runtime_bindings_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX ella_runtime_bindings_user_role_provider_key
    ON ella_runtime_bindings(user_id, role, provider);
"""


async def _run_with_database(scenario):
    schema = f"authority_lock_{uuid.uuid4().hex}"
    admin = await asyncpg.connect(TEST_DSN)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(
        TEST_DSN,
        min_size=1,
        max_size=6,
        server_settings={"search_path": schema},
    )
    previous_voice_pool = voice_canary._pool
    voice_canary._pool = pool
    try:
        async with pool.acquire() as conn:
            await conn.execute(BASE_PROVISIONING_SCHEMA)
            for name in (
                "008_create_voice_canary_controls.sql",
                "009_create_hermes_cloud_runtime_pool.sql",
                "010_add_cloud_profile_class.sql",
                "011_create_invitation_redemption.sql",
                "012_create_account_profile_runtime_targets.sql",
                "013_create_managed_cloud_consent_authority.sql",
                "014_add_synthetic_invitation_operator_audit.sql",
                "015_add_invitation_allowed_email_hash.sql",
                "017_add_provider_attempt_deletion_fence.sql",
            ):
                await conn.execute((MIGRATIONS / name).read_text(encoding="utf-8"))
        await scenario(pool)
    finally:
        voice_canary._pool = previous_voice_pool
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def _seed_grant(pool, uid):
    async with pool.acquire() as conn:
        user_id = await conn.fetchval(
            """
            INSERT INTO users (omi_uid, profile_class)
            VALUES ($1, 'synthetic')
            RETURNING id
            """,
            uid,
        )
        await conn.execute(
            """
            INSERT INTO voice_entitlements (
                uid, status, provider_allowlist, model_allowlist
            ) VALUES (
                $1, 'active', ARRAY['hermes_cloud'], ARRAY['gpt-5.6-terra']
            )
            """,
            uid,
        )
        await conn.execute(
            """
            INSERT INTO ella_managed_cloud_consent_authority (
                user_id, decision, consent_receipt_ref, profile_binding_id,
                policy_version, processor_set_hash, scope_version, scope_hash
            ) VALUES (
                $1, 'granted', $2, 'synthetic-profile', 'ai-data-processors-v8',
                $3, 'managed-cloud-internal-pilot-v2', $4
            )
            """,
            user_id,
            "sha256:" + ("1" * 64),
            "sha256:" + ("2" * 64),
            "sha256:" + ("3" * 64),
        )
    return authority_advisory_lock.AuthorityOwner.from_values(user_id, user_id)


def _prompt_receipt() -> dict[str, Any]:
    return {
        "schema_version": "ella-hermes-cloud-approval-v1",
        "prompt_pack_version": "prompt-v1",
        "model_policy_version": "model-policy-v1",
        "expected_model": "gpt-5.6-terra",
        "model_context_window_tokens": 16384,
        "policy_commit_sha": "a" * 40,
        "lane_s_review_url": "https://github.com/ellaaicare/ella-ai/pull/1124",
        "approval_manifest_sha256": "b" * 64,
        "content_free": True,
        "soul_sha256": "c" * 64,
        "observed_soul_sha256": "c" * 64,
        "agents_sha256": "d" * 64,
        "observed_agents_sha256": "d" * 64,
        "model_policy_sha256": "e" * 64,
        "observed_model_policy_sha256": "e" * 64,
    }


def _managed_cloud_grant(uid: str, *, revision: str = "one") -> managed_cloud_consent.ManagedCloudGrant:
    return managed_cloud_consent.ManagedCloudGrant(
        account_uid=uid,
        profile_uid=uid,
        consent_receipt_id=f"synthetic-receipt-{revision}",
        profile_binding_id=f"synthetic-profile-{revision}",
        policy_version=LINEAGE.policy_version,
        processor_set_hash=LINEAGE.processor_set_hash,
        scope_version=LINEAGE.scope_version,
        scope_hash=LINEAGE.scope_hash,
    )


async def _wait_for_advisory_waiter(
    pool: asyncpg.Pool,
    owner: authority_advisory_lock.AuthorityOwner,
) -> None:
    key = authority_advisory_lock.authority_lock_key(
        str(owner.account_id),
        str(owner.profile_id),
    )
    class_id, object_id = authority_advisory_lock._advisory_lock_parts(key)
    for _attempt in range(100):
        async with pool.acquire() as observer:
            waiting = await observer.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_locks
                    WHERE locktype = 'advisory'
                      AND classid::bigint = $1
                      AND objid::bigint = $2
                      AND objsubid = 1
                      AND mode = 'ExclusiveLock'
                      AND NOT granted
                )
                """,
                class_id,
                object_id,
            )
        if waiting:
            return
        await asyncio.sleep(0.02)
    pytest.fail("production writer never waited on the shared v1 authority lock")


@dataclass
class _WriterCase:
    owner: authority_advisory_lock.AuthorityOwner
    writer: Callable[[], Awaitable[Any]]
    snapshot: Callable[[], Awaitable[Any]]
    expected_before: Any
    expected_after: Any


async def _assert_serialized_writer(
    pool: asyncpg.Pool,
    case: _WriterCase,
) -> None:
    assert await case.snapshot() == case.expected_before
    async with pool.acquire() as broker_conn:
        transaction = broker_conn.transaction()
        await transaction.start()
        await authority_advisory_lock.acquire_authority_lock(
            broker_conn,
            owner=case.owner,
        )
        writer = asyncio.create_task(case.writer())
        await _wait_for_advisory_waiter(pool, case.owner)
        assert not writer.done()
        assert await case.snapshot() == case.expected_before
        await transaction.commit()
    await asyncio.wait_for(writer, timeout=5)
    assert await case.snapshot() == case.expected_after


async def _seed_cloud_writer(
    pool: asyncpg.Pool,
    *,
    uid: str,
    instance: str,
) -> tuple[
    EllaProvisioningRepository,
    authority_advisory_lock.AuthorityOwner,
    str,
    int,
]:
    async with pool.acquire() as conn:
        user_id = await conn.fetchval(
            """
            INSERT INTO users (omi_uid, email, profile_class)
            VALUES ($1, $2, 'synthetic')
            RETURNING id
            """,
            uid,
            f"{uid}@example.invalid",
        )
        job_id = await conn.fetchval(
            """
            INSERT INTO ella_provisioning_jobs (
                user_id, target_schema_version
            ) VALUES ($1, 'hermes-cloud-user-v1')
            RETURNING id
            """,
            user_id,
        )
        await conn.execute(
            """
            INSERT INTO voice_entitlements (
                uid, status, provider_allowlist, model_allowlist,
                mode_allowlist, consent_policy_version,
                consent_processor_set_hash, consent_scope_version,
                consent_scope_hash
            ) VALUES (
                $1, 'active', ARRAY['hermes_cloud'],
                ARRAY['gpt-5.6-terra'],
                ARRAY[
                    'hermes-cloud-chat', 'hermes-cloud-voice',
                    'hermes-cloud-transcript', 'hermes-cloud-guardian',
                    'hermes-cloud-photon'
                ],
                $2, $3, $4, $5
            )
            """,
            uid,
            LINEAGE.policy_version,
            LINEAGE.processor_set_hash,
            LINEAGE.scope_version,
            LINEAGE.scope_hash,
        )
    repository = EllaProvisioningRepository(pool)
    await repository.register_cloud_pool_binding(
        runtime_instance_id=instance,
        profile_name=f"profile-{instance}",
        agent_id="hermes-cloud",
        api_base_url_ref="env:ELLA_HERMES_CLOUD_API_URL_SYNTHETIC",
        api_key_ref="env:ELLA_HERMES_CLOUD_API_KEY_SYNTHETIC",
        honcho_api_key_ref=None,
        template_version="template-v1",
        prompt_pack_version="prompt-v1",
        prompt_artifact_receipt=_prompt_receipt(),
        model_policy_version="model-policy-v1",
        voice_policy_version="voice-policy-v1",
        expected_model="gpt-5.6-terra",
        allowed_tools=[],
        required_capabilities=["responses_api"],
        health_receipt={"status": "ok", "content_free": True},
    )
    admission = await voice_canary.evaluate_runtime_activation(
        uid=uid,
        provider="hermes_cloud",
        model="gpt-5.6-terra",
    )
    assert admission.allowed
    return (
        repository,
        authority_advisory_lock.AuthorityOwner.from_values(user_id, user_id),
        str(job_id),
        int(admission.entitlement["revision"]),
    )


def test_broker_lock_blocks_omi_revoke_until_release_without_partial_mutation():
    async def scenario(pool):
        uid = "synthetic-broker-blocks-revoke"
        owner = await _seed_grant(pool, uid)
        async with pool.acquire() as broker_conn:
            transaction = broker_conn.transaction()
            await transaction.start()
            await authority_advisory_lock.acquire_authority_lock(
                broker_conn,
                owner=owner,
            )
            revoke = asyncio.create_task(
                managed_cloud_consent.synchronize_denial(
                    uid=uid,
                    decision="revoked",
                )
            )
            await asyncio.sleep(0.15)
            assert not revoke.done()
            async with pool.acquire() as observer:
                assert (
                    await observer.fetchval(
                        """
                        SELECT decision
                        FROM ella_managed_cloud_consent_authority
                        WHERE user_id = $1
                        """,
                        owner.account_id,
                    )
                    == "granted"
                )
                assert (
                    await observer.fetchval(
                        "SELECT status FROM voice_entitlements WHERE uid = $1",
                        uid,
                    )
                    == "active"
                )
            await transaction.commit()
            result = await asyncio.wait_for(revoke, timeout=3)
            assert result["decision"] == "revoked"

    asyncio.run(_run_with_database(scenario))


def test_omi_revoke_lock_blocks_broker_and_releases_without_deadlock():
    async def scenario(pool):
        uid = "synthetic-revoke-blocks-broker"
        owner = await _seed_grant(pool, uid)
        key = authority_advisory_lock.authority_lock_key(
            str(owner.account_id),
            str(owner.profile_id),
        )
        async with pool.acquire() as conn:
            await conn.execute(
                """
                CREATE FUNCTION hold_authority_revoke() RETURNS trigger AS $$
                BEGIN
                    PERFORM pg_sleep(0.6);
                    RETURN NEW;
                END
                $$ LANGUAGE plpgsql
                """
            )
            await conn.execute(
                """
                CREATE TRIGGER hold_authority_revoke
                BEFORE UPDATE ON ella_managed_cloud_consent_authority
                FOR EACH ROW EXECUTE FUNCTION hold_authority_revoke()
                """
            )

        revoke = asyncio.create_task(
            managed_cloud_consent.synchronize_denial(
                uid=uid,
                decision="revoked",
            )
        )
        for _attempt in range(40):
            async with pool.acquire() as probe:
                acquired = await probe.fetchval(
                    "SELECT pg_try_advisory_lock($1::bigint)",
                    key,
                )
                if acquired:
                    await probe.execute(
                        "SELECT pg_advisory_unlock($1::bigint)",
                        key,
                    )
                else:
                    break
            await asyncio.sleep(0.025)
        else:
            pytest.fail("OMI writer never acquired the shared v1 lock")

        broker_acquired = asyncio.Event()

        async def broker_waiter():
            async with pool.acquire() as broker_conn:
                async with broker_conn.transaction():
                    await authority_advisory_lock.acquire_authority_lock(
                        broker_conn,
                        owner=owner,
                    )
                    broker_acquired.set()

        broker = asyncio.create_task(broker_waiter())
        await asyncio.sleep(0.1)
        assert not broker_acquired.is_set()
        result = await asyncio.wait_for(revoke, timeout=3)
        assert result["decision"] == "revoked"
        await asyncio.wait_for(broker, timeout=3)
        assert broker_acquired.is_set()

    asyncio.run(_run_with_database(scenario))


def test_account_profile_lock_isolation_allows_unrelated_writer():
    async def scenario(pool):
        uid_a = "synthetic-lock-a"
        uid_b = "synthetic-lock-b"
        owner_a = await _seed_grant(pool, uid_a)
        await _seed_grant(pool, uid_b)
        async with pool.acquire() as conn:
            transaction = conn.transaction()
            await transaction.start()
            await authority_advisory_lock.acquire_authority_lock(
                conn,
                owner=owner_a,
            )
            result = await asyncio.wait_for(
                managed_cloud_consent.synchronize_denial(
                    uid=uid_b,
                    decision="revoked",
                ),
                timeout=2,
            )
            assert result["decision"] == "revoked"
            await transaction.rollback()

    asyncio.run(_run_with_database(scenario))


async def _prepare_writer_case(
    pool: asyncpg.Pool,
    name: str,
) -> _WriterCase:
    uid = f"synthetic-writer-{name.replace('_', '-')}"
    repository = EllaProvisioningRepository(pool)

    if name == "consent_grant":
        async with pool.acquire() as conn:
            user_id = await conn.fetchval(
                "INSERT INTO users (omi_uid, email, profile_class) VALUES ($1, $2, 'synthetic') RETURNING id",
                uid,
                f"{uid}@example.invalid",
            )
        owner = authority_advisory_lock.AuthorityOwner.from_values(user_id, user_id)

        async def snapshot():
            return await pool.fetchval(
                "SELECT decision FROM ella_managed_cloud_consent_authority WHERE user_id = $1",
                user_id,
            )

        return _WriterCase(
            owner=owner,
            writer=lambda: managed_cloud_consent.synchronize_grant(
                grant=_managed_cloud_grant(uid),
            ),
            snapshot=snapshot,
            expected_before=None,
            expected_after="granted",
        )

    if name == "consent_regrant":
        owner = await _seed_grant(pool, uid)

        async def snapshot():
            row = await pool.fetchrow(
                """
                SELECT a.profile_binding_id, a.revision,
                       (
                           SELECT status
                           FROM voice_entitlements
                           WHERE uid = $2
                       ) AS entitlement_status
                FROM ella_managed_cloud_consent_authority a
                WHERE a.user_id = $1
                """,
                owner.account_id,
                uid,
            )
            return tuple(row.values())

        return _WriterCase(
            owner=owner,
            writer=lambda: managed_cloud_consent.synchronize_grant(
                grant=_managed_cloud_grant(uid, revision="two"),
            ),
            snapshot=snapshot,
            expected_before=("synthetic-profile", 1, "active"),
            expected_after=("synthetic-profile-two", 2, "revoked"),
        )

    if name in {"entitlement_upsert", "entitlement_status", "entitlement_delete"}:
        async with pool.acquire() as conn:
            user_id = await conn.fetchval(
                "INSERT INTO users (omi_uid, email, profile_class) VALUES ($1, $2, 'synthetic') RETURNING id",
                uid,
                f"{uid}@example.invalid",
            )
            if name != "entitlement_upsert":
                await conn.execute(
                    "INSERT INTO voice_entitlements (uid, status) VALUES ($1, 'active')",
                    uid,
                )
        owner = authority_advisory_lock.AuthorityOwner.from_values(user_id, user_id)

        if name == "entitlement_upsert":

            async def snapshot():
                row = await pool.fetchrow(
                    "SELECT status, plan FROM voice_entitlements WHERE uid = $1",
                    uid,
                )
                return tuple(row.values()) if row else None

            return _WriterCase(
                owner=owner,
                writer=lambda: voice_canary.upsert_entitlement(
                    uid=uid,
                    plan="synthetic",
                    daily_limit_s=300,
                    monthly_limit_s=3000,
                    max_session_s=120,
                    max_concurrent=1,
                    soft_limit_ratio=0.8,
                    provider_allowlist=["hermes_cloud"],
                    mode_allowlist=["hermes-cloud-chat"],
                    fallback_policy={"enabled": False, "order": []},
                    operator_note="synthetic authority writer probe",
                ),
                snapshot=snapshot,
                expected_before=None,
                expected_after=("active", "synthetic"),
            )

        async def entitlement_snapshot():
            return await pool.fetchval(
                "SELECT status FROM voice_entitlements WHERE uid = $1",
                uid,
            )

        if name == "entitlement_status":
            return _WriterCase(
                owner=owner,
                writer=lambda: voice_canary.update_entitlement_status(
                    uid=uid,
                    status="suspended",
                    operator_note="synthetic authority writer probe",
                ),
                snapshot=entitlement_snapshot,
                expected_before="active",
                expected_after="suspended",
            )

        async def entitlement_count():
            return int(
                await pool.fetchval(
                    "SELECT COUNT(*) FROM voice_entitlements WHERE uid = $1",
                    uid,
                )
            )

        return _WriterCase(
            owner=owner,
            writer=lambda: voice_canary.delete_user_voice_data(uid),
            snapshot=entitlement_count,
            expected_before=1,
            expected_after=0,
        )

    if name in {"identity_create", "identity_update", "identity_bind", "user_activate"}:
        email = f"{uid}@example.invalid"
        if name == "identity_create":
            owner = authority_advisory_lock.provisional_identity_owner(uid)

            async def snapshot():
                return await pool.fetchval(
                    "SELECT id FROM users WHERE omi_uid = $1",
                    uid,
                )

            return _WriterCase(
                owner=owner,
                writer=lambda: repository.ensure_user_identity(
                    uid=uid,
                    email=email,
                    name="Synthetic Created",
                    timezone_name="UTC",
                ),
                snapshot=snapshot,
                expected_before=None,
                expected_after=owner.account_id,
            )

        async with pool.acquire() as conn:
            if name == "identity_bind":
                user_id = await conn.fetchval(
                    """
                    INSERT INTO users (
                        email, name, status, profile_class
                    ) VALUES ($1, 'Synthetic Before', 'ACTIVE', 'synthetic')
                    RETURNING id
                    """,
                    email,
                )
            else:
                initial_status = "ACTIVE" if name == "identity_update" else "PENDING"
                user_id = await conn.fetchval(
                    """
                    INSERT INTO users (
                        omi_uid, email, name, status, profile_class
                    ) VALUES ($1, $2, 'Synthetic Before', $3, 'synthetic')
                    RETURNING id
                    """,
                    uid,
                    email,
                    initial_status,
                )
        owner = authority_advisory_lock.AuthorityOwner.from_values(user_id, user_id)
        if name == "identity_bind":

            async def snapshot():
                row = await pool.fetchrow(
                    """
                    SELECT omi_uid, name, timezone, identities ->> 'omi_uid'
                    FROM users
                    WHERE id = $1
                    """,
                    user_id,
                )
                return tuple(row.values())

            return _WriterCase(
                owner=owner,
                writer=lambda: repository.ensure_user_identity(
                    uid=uid,
                    email=email,
                    name="Synthetic Bound",
                    timezone_name="America/Los_Angeles",
                ),
                snapshot=snapshot,
                expected_before=(None, "Synthetic Before", "UTC", None),
                expected_after=(
                    uid,
                    "Synthetic Bound",
                    "America/Los_Angeles",
                    uid,
                ),
            )

        if name == "identity_update":

            async def snapshot():
                return await pool.fetchval(
                    "SELECT name FROM users WHERE id = $1",
                    user_id,
                )

            return _WriterCase(
                owner=owner,
                writer=lambda: repository.ensure_user_identity(
                    uid=uid,
                    email=email,
                    name="Synthetic After",
                    timezone_name="UTC",
                ),
                snapshot=snapshot,
                expected_before="Synthetic Before",
                expected_after="Synthetic After",
            )

        async def status_snapshot():
            return await pool.fetchval(
                "SELECT status FROM users WHERE id = $1",
                user_id,
            )

        return _WriterCase(
            owner=owner,
            writer=lambda: repository.activate_user(uid),
            snapshot=status_snapshot,
            expected_before="PENDING",
            expected_after="ACTIVE",
        )

    if name in {"runtime_stage", "runtime_activate"}:
        async with pool.acquire() as conn:
            user_id = await conn.fetchval(
                """
                INSERT INTO users (
                    omi_uid, email, status, profile_class
                ) VALUES ($1, $2, 'PENDING', 'synthetic')
                RETURNING id
                """,
                uid,
                f"{uid}@example.invalid",
            )
            if name == "runtime_activate":
                await conn.execute(
                    """
                    INSERT INTO ella_runtime_bindings (
                        user_id, role, provider, profile_name, agent_id,
                        template_version, model_policy_version,
                        voice_policy_version, health_state, active
                    ) VALUES (
                        $1, 'user', 'hermes', $2, 'synthetic-agent',
                        'template-v1', 'model-policy-v1',
                        'voice-policy-v1', 'healthy', false
                    )
                    """,
                    user_id,
                    f"profile-{uid}",
                )
        owner = authority_advisory_lock.AuthorityOwner.from_values(user_id, user_id)
        if name == "runtime_stage":

            async def snapshot():
                return int(
                    await pool.fetchval(
                        "SELECT COUNT(*) FROM ella_runtime_bindings WHERE user_id = $1",
                        user_id,
                    )
                )

            return _WriterCase(
                owner=owner,
                writer=lambda: repository.stage_runtime_binding(
                    uid=uid,
                    binding={
                        "provider": "hermes",
                        "profile_name": f"profile-{uid}",
                        "agent_id": "synthetic-agent",
                        "template_version": "template-v1",
                        "model_policy_version": "model-policy-v1",
                        "voice_policy_version": "voice-policy-v1",
                    },
                ),
                snapshot=snapshot,
                expected_before=0,
                expected_after=1,
            )

        async def activation_snapshot():
            row = await pool.fetchrow(
                """
                SELECT b.active, u.status
                FROM ella_runtime_bindings b
                JOIN users u ON u.id = b.user_id
                WHERE b.user_id = $1 AND b.provider = 'hermes'
                """,
                user_id,
            )
            return tuple(row.values())

        return _WriterCase(
            owner=owner,
            writer=lambda: repository.activate_runtime_binding(
                uid=uid,
                provider="hermes",
            ),
            snapshot=activation_snapshot,
            expected_before=(False, "PENDING"),
            expected_after=(True, "ACTIVE"),
        )

    if name in {"cloud_claim", "cloud_finalize", "cloud_quarantine", "cloud_promote"}:
        repository, owner, job_id, revision = await _seed_cloud_writer(
            pool,
            uid=uid,
            instance=f"instance-{name}",
        )

        if name == "cloud_claim":

            async def snapshot():
                return await pool.fetchval(
                    "SELECT status FROM ella_runtime_bindings WHERE runtime_instance_id = $1",
                    f"instance-{name}",
                )

            return _WriterCase(
                owner=owner,
                writer=lambda: repository.claim_cloud_pool_binding(
                    uid=uid,
                    job_id=job_id,
                    lease_seconds=120,
                    admitted_entitlement_revision=revision,
                    provider="hermes_cloud",
                    model="gpt-5.6-terra",
                    required_profile_class="synthetic",
                ),
                snapshot=snapshot,
                expected_before="pool_available",
                expected_after="claiming",
            )

        claimed = await repository.claim_cloud_pool_binding(
            uid=uid,
            job_id=job_id,
            lease_seconds=120,
            admitted_entitlement_revision=revision,
            provider="hermes_cloud",
            model="gpt-5.6-terra",
            required_profile_class="synthetic",
        )
        claim_token = str(claimed["claim_token"])
        if name == "cloud_finalize":

            async def snapshot():
                row = await pool.fetchrow(
                    """
                    SELECT b.status, COUNT(t.id)::integer AS target_count
                    FROM ella_runtime_bindings b
                    LEFT JOIN ella_runtime_targets t ON t.runtime_binding_id = b.id
                    WHERE b.id = $1
                    GROUP BY b.status
                    """,
                    claimed["id"],
                )
                return tuple(row.values())

            return _WriterCase(
                owner=owner,
                writer=lambda: repository.finalize_cloud_pool_claim(
                    uid=uid,
                    job_id=job_id,
                    claim_token=claim_token,
                    admitted_entitlement_revision=revision,
                    authority_lineage=LINEAGE,
                    status="internal_canary",
                    health_receipt={
                        "status": "ok",
                        "content_free": True,
                        **LINEAGE.as_dict(),
                        "admission_revision": revision,
                    },
                ),
                snapshot=snapshot,
                expected_before=("claiming", 0),
                expected_after=("internal_canary", 5),
            )

        if name == "cloud_quarantine":

            async def snapshot():
                return await pool.fetchval(
                    "SELECT status FROM ella_runtime_bindings WHERE id = $1",
                    claimed["id"],
                )

            return _WriterCase(
                owner=owner,
                writer=lambda: repository.quarantine_cloud_pool_claim(
                    uid=uid,
                    job_id=job_id,
                    claim_token=claim_token,
                    reason="synthetic authority writer probe",
                    health_receipt={"content_free": True},
                ),
                snapshot=snapshot,
                expected_before="claiming",
                expected_after="quarantined",
            )

        finalized = await repository.finalize_cloud_pool_claim(
            uid=uid,
            job_id=job_id,
            claim_token=claim_token,
            admitted_entitlement_revision=revision,
            authority_lineage=LINEAGE,
            status="shadow",
            health_receipt={
                "status": "ok",
                "content_free": True,
                **LINEAGE.as_dict(),
                "admission_revision": revision,
            },
        )

        async def snapshot():
            row = await pool.fetchrow(
                "SELECT status, active FROM ella_runtime_bindings WHERE id = $1",
                finalized["id"],
            )
            return tuple(row.values())

        return _WriterCase(
            owner=owner,
            writer=lambda: repository.promote_cloud_binding(
                uid=uid,
                binding_id=str(finalized["id"]),
                expected_revision=int(finalized["revision"]),
                target_status="internal_canary",
                required_profile_class="synthetic",
                admitted_entitlement_revision=revision,
                authority_lineage=LINEAGE,
            ),
            snapshot=snapshot,
            expected_before=("shadow", False),
            expected_after=("internal_canary", True),
        )

    raise AssertionError(f"unhandled writer case: {name}")


@pytest.mark.parametrize(
    "name",
    (
        "consent_grant",
        "consent_regrant",
        "entitlement_upsert",
        "entitlement_status",
        "entitlement_delete",
        "identity_create",
        "identity_update",
        "identity_bind",
        "user_activate",
        "runtime_stage",
        "runtime_activate",
        "cloud_claim",
        "cloud_finalize",
        "cloud_quarantine",
        "cloud_promote",
    ),
)
def test_each_production_writer_waits_before_mutation_and_commits_after_release(name):
    async def scenario(pool):
        case = await _prepare_writer_case(pool, name)
        await _assert_serialized_writer(pool, case)

    asyncio.run(_run_with_database(scenario))


def test_content_admission_releases_pool_before_same_owner_nested_writers_at_full_capacity():
    async def scenario(pool):
        capacity = 6
        users = []
        async with pool.acquire() as seed:
            for index in range(capacity):
                uid = f"synthetic-content-reentry-{index}"
                user_id = await seed.fetchval(
                    """
                    INSERT INTO users (omi_uid, email, profile_class, status)
                    VALUES ($1, $2, 'synthetic', 'ACTIVE')
                    RETURNING id
                    """,
                    uid,
                    f"{uid}@example.invalid",
                )
                users.append((uid, user_id))

        nested_count = 0
        nested_lock = asyncio.Lock()
        all_nested = asyncio.Event()

        async def mounted_request(uid, user_id):
            nonlocal nested_count
            await content_write_fence._assert_postgres_owner_active(uid)
            owner = authority_advisory_lock.AuthorityOwner.from_values(user_id, user_id)
            async with pool.acquire() as nested_connection:
                async with nested_connection.transaction():
                    proof = await authority_advisory_lock.acquire_authority_lock(
                        nested_connection,
                        owner=owner,
                    )
                    assert (
                        await authority_advisory_lock.verify_self_owner_after_lock(
                            nested_connection,
                            uid=uid,
                            owner=owner,
                            proof=proof,
                        )
                        == user_id
                    )
                    async with nested_lock:
                        nested_count += 1
                        if nested_count == capacity:
                            all_nested.set()
                    await asyncio.wait_for(all_nested.wait(), timeout=5)

        await asyncio.wait_for(
            asyncio.gather(*(mounted_request(uid, user_id) for uid, user_id in users)),
            timeout=10,
        )
        assert nested_count == capacity

        blocked_uid, blocked_user_id = users[0]
        async with pool.acquire() as tombstone:
            await tombstone.execute(
                "UPDATE users SET status = 'DELETION_PENDING' WHERE id = $1",
                blocked_user_id,
            )
        with pytest.raises(content_write_fence.ContentWriteFenceError) as forbidden:
            await content_write_fence._assert_postgres_owner_active(blocked_uid)
        assert forbidden.value.code == "account_write_forbidden"

    asyncio.run(_run_with_database(scenario))


def _authority_error_code(error: BaseException) -> str | None:
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, authority_advisory_lock.AuthorityLockError):
            return current.code
        current = current.__cause__ or current.__context__
    return None


@pytest.mark.parametrize(
    "name",
    (
        "consent_grant",
        "consent_regrant",
        "entitlement_upsert",
        "entitlement_status",
        "entitlement_delete",
        "user_activate",
        "runtime_stage",
        "runtime_activate",
        "cloud_claim",
        "cloud_finalize",
        "cloud_quarantine",
        "cloud_promote",
    ),
)
def test_each_existing_owner_writer_reproves_owner_and_rolls_back_on_drift(name):
    async def scenario(pool):
        uid = f"synthetic-writer-{name.replace('_', '-')}"
        case = await _prepare_writer_case(pool, name)
        async with pool.acquire() as broker:
            transaction = broker.transaction()
            await transaction.start()
            await authority_advisory_lock.acquire_authority_lock(
                broker,
                owner=case.owner,
            )
            writer = asyncio.create_task(case.writer())
            await _wait_for_advisory_waiter(pool, case.owner)
            assert not writer.done()
            async with pool.acquire() as drift:
                async with drift.transaction():
                    await drift.execute(
                        """
                        UPDATE users
                        SET omi_uid = $2, email = $3
                        WHERE id = $1
                        """,
                        case.owner.account_id,
                        f"{uid}-moved",
                        f"{uid}-moved@example.invalid",
                    )
                    await drift.execute(
                        """
                        INSERT INTO users (
                            omi_uid, email, name, status, profile_class
                        ) VALUES (
                            $1, $2, 'Replacement Owner', 'PENDING', 'synthetic'
                        )
                        """,
                        uid,
                        f"{uid}@replacement.invalid",
                    )
            await transaction.commit()

        with pytest.raises(Exception) as raised:
            await asyncio.wait_for(writer, timeout=5)
        assert _authority_error_code(raised.value) == "authority_lock_owner_drift"
        assert await case.snapshot() == case.expected_before

    asyncio.run(_run_with_database(scenario))


def test_authority_lock_proof_is_connection_and_transaction_bound():
    async def scenario(pool):
        uid = "synthetic-proof-lifecycle"
        owner = await _seed_grant(pool, uid)

        async with pool.acquire() as first:
            committed = first.transaction()
            await committed.start()
            proof = await authority_advisory_lock.acquire_authority_lock(
                first,
                owner=owner,
            )
            await authority_advisory_lock.require_authority_lock(
                first,
                proof,
                owner=owner,
            )

            async with pool.acquire() as other:
                with pytest.raises(
                    authority_advisory_lock.AuthorityLockError,
                    match="authority_lock_proof_connection_mismatch",
                ):
                    await managed_cloud_consent._quarantine_on_connection(
                        other,
                        uid=uid,
                        user_id=owner.account_id,
                        reason="synthetic_cross_connection_probe",
                        owner_lock=proof,
                    )

            await committed.commit()
            with pytest.raises(
                authority_advisory_lock.AuthorityLockError,
                match="authority_lock_proof_transaction_stale",
            ):
                await managed_cloud_consent._quarantine_on_connection(
                    first,
                    uid=uid,
                    user_id=owner.account_id,
                    reason="synthetic_post_commit_probe",
                    owner_lock=proof,
                )

            rolled_back = first.transaction()
            await rolled_back.start()
            rollback_proof = await authority_advisory_lock.acquire_authority_lock(
                first,
                owner=owner,
            )
            await rolled_back.rollback()
            with pytest.raises(
                authority_advisory_lock.AuthorityLockError,
                match="authority_lock_proof_transaction_stale",
            ):
                await managed_cloud_consent._quarantine_on_connection(
                    first,
                    uid=uid,
                    user_id=owner.account_id,
                    reason="synthetic_post_rollback_probe",
                    owner_lock=rollback_proof,
                )

        async with pool.acquire() as observer:
            assert (
                await observer.fetchval(
                    """
                    SELECT decision
                    FROM ella_managed_cloud_consent_authority
                    WHERE user_id = $1
                    """,
                    owner.account_id,
                )
                == "granted"
            )
            assert (
                await observer.fetchval(
                    "SELECT status FROM voice_entitlements WHERE uid = $1",
                    uid,
                )
                == "active"
            )

    asyncio.run(_run_with_database(scenario))


def test_forged_proof_cannot_reach_proof_gated_production_mutation():
    async def scenario(pool):
        uid = "synthetic-forged-proof"
        owner = await _seed_grant(pool, uid)
        forged = object.__new__(authority_advisory_lock.AuthorityLockProof)

        async with pool.acquire() as conn:
            async with conn.transaction():
                with pytest.raises(
                    authority_advisory_lock.AuthorityLockError,
                    match="authority_lock_proof_forged",
                ):
                    await managed_cloud_consent._quarantine_on_connection(
                        conn,
                        uid=uid,
                        user_id=owner.account_id,
                        reason="synthetic_forgery_probe",
                        owner_lock=forged,
                    )

        async with pool.acquire() as observer:
            assert (
                await observer.fetchval(
                    """
                    SELECT decision
                    FROM ella_managed_cloud_consent_authority
                    WHERE user_id = $1
                    """,
                    owner.account_id,
                )
                == "granted"
            )
            assert (
                await observer.fetchval(
                    "SELECT status FROM voice_entitlements WHERE uid = $1",
                    uid,
                )
                == "active"
            )

    asyncio.run(_run_with_database(scenario))


def test_synchronize_denial_owner_drift_after_unlocked_lookup_fails_closed_before_mutation(monkeypatch):
    async def scenario(pool):
        uid = "synthetic-owner-drift"
        original_owner = await _seed_grant(pool, uid)
        candidate_resolved = asyncio.Event()
        original_resolver = authority_advisory_lock.resolve_self_owner_unlocked

        async def resolve_and_signal(connection, *, uid):
            owner = await original_resolver(connection, uid=uid)
            candidate_resolved.set()
            return owner

        monkeypatch.setattr(
            authority_advisory_lock,
            "resolve_self_owner_unlocked",
            resolve_and_signal,
        )

        async with pool.acquire() as broker_conn:
            broker_transaction = broker_conn.transaction()
            await broker_transaction.start()
            await authority_advisory_lock.acquire_authority_lock(
                broker_conn,
                owner=original_owner,
            )
            denial = asyncio.create_task(
                managed_cloud_consent.synchronize_denial(
                    uid=uid,
                    decision="revoked",
                )
            )
            await asyncio.wait_for(candidate_resolved.wait(), timeout=2)
            await asyncio.sleep(0.05)
            assert not denial.done()

            async with pool.acquire() as owner_writer:
                async with owner_writer.transaction():
                    await owner_writer.execute(
                        "UPDATE users SET omi_uid = $2 WHERE id = $1",
                        original_owner.account_id,
                        f"{uid}-moved",
                    )
                    replacement_id = await owner_writer.fetchval(
                        """
                        INSERT INTO users (omi_uid, profile_class)
                        VALUES ($1, 'synthetic')
                        RETURNING id
                        """,
                        uid,
                    )

            await broker_transaction.commit()
            with pytest.raises(
                managed_cloud_consent.ManagedCloudAuthorityUnavailable,
                match="managed_cloud_authority_unavailable",
            ) as raised:
                await asyncio.wait_for(denial, timeout=3)
            assert isinstance(
                raised.value.__cause__,
                authority_advisory_lock.AuthorityLockError,
            )
            assert raised.value.__cause__.code == "authority_lock_owner_drift"

        async with pool.acquire() as observer:
            assert (
                await observer.fetchval(
                    """
                    SELECT COUNT(*)
                    FROM ella_managed_cloud_consent_authority
                    WHERE user_id = $1
                    """,
                    replacement_id,
                )
                == 0
            )
            assert (
                await observer.fetchval(
                    """
                    SELECT decision
                    FROM ella_managed_cloud_consent_authority
                    WHERE user_id = $1
                    """,
                    original_owner.account_id,
                )
                == "granted"
            )
            assert (
                await observer.fetchval(
                    "SELECT status FROM voice_entitlements WHERE uid = $1",
                    uid,
                )
                == "active"
            )

    asyncio.run(_run_with_database(scenario))


@pytest.mark.parametrize("writer_name", ("ensure_user_identity", "activate_user"))
def test_users_writer_owner_drift_fails_closed_with_zero_protected_write(writer_name):
    async def scenario(pool):
        uid = f"synthetic-users-drift-{writer_name}"
        email = f"{uid}@example.invalid"
        async with pool.acquire() as conn:
            original_id = await conn.fetchval(
                """
                INSERT INTO users (
                    omi_uid, email, name, status, profile_class
                ) VALUES (
                    $1, $2, 'Original Owner', 'PENDING', 'synthetic'
                )
                RETURNING id
                """,
                uid,
                email,
            )
        owner = authority_advisory_lock.AuthorityOwner.from_values(
            original_id,
            original_id,
        )
        repository = EllaProvisioningRepository(pool)
        async with pool.acquire() as broker:
            transaction = broker.transaction()
            await transaction.start()
            await authority_advisory_lock.acquire_authority_lock(
                broker,
                owner=owner,
            )
            if writer_name == "ensure_user_identity":
                writer = asyncio.create_task(
                    repository.ensure_user_identity(
                        uid=uid,
                        email=email,
                        name="Unauthorized Update",
                        timezone_name="UTC",
                    )
                )
            else:
                writer = asyncio.create_task(repository.activate_user(uid))

            await _wait_for_advisory_waiter(pool, owner)
            assert not writer.done()
            async with pool.acquire() as drift:
                async with drift.transaction():
                    await drift.execute(
                        """
                        UPDATE users
                        SET omi_uid = $2, email = $3
                        WHERE id = $1
                        """,
                        original_id,
                        f"{uid}-moved",
                        f"{uid}-moved@example.invalid",
                    )
                    replacement_id = await drift.fetchval(
                        """
                        INSERT INTO users (
                            omi_uid, email, name, status, profile_class
                        ) VALUES (
                            $1, $2, 'Replacement Owner', 'PENDING', 'synthetic'
                        )
                        RETURNING id
                        """,
                        uid,
                        email,
                    )
            await transaction.commit()

        with pytest.raises(
            authority_advisory_lock.AuthorityLockError,
            match="authority_lock_owner_drift",
        ):
            await asyncio.wait_for(writer, timeout=5)

        async with pool.acquire() as observer:
            original = await observer.fetchrow(
                "SELECT name, status FROM users WHERE id = $1",
                original_id,
            )
            replacement = await observer.fetchrow(
                "SELECT name, status FROM users WHERE id = $1",
                replacement_id,
            )
        assert tuple(original.values()) == ("Original Owner", "PENDING")
        assert tuple(replacement.values()) == ("Replacement Owner", "PENDING")

    asyncio.run(_run_with_database(scenario))


def test_identity_bind_owner_drift_rolls_back_without_partial_write_and_releases_lock():
    async def scenario(pool):
        uid = "synthetic-users-drift-identity-bind"
        email = f"{uid}@example.invalid"
        async with pool.acquire() as conn:
            original_id = await conn.fetchval(
                """
                INSERT INTO users (
                    email, name, status, profile_class
                ) VALUES (
                    $1, 'Original Email Owner', 'PENDING', 'synthetic'
                )
                RETURNING id
                """,
                email,
            )
        owner = authority_advisory_lock.AuthorityOwner.from_values(
            original_id,
            original_id,
        )
        repository = EllaProvisioningRepository(pool)

        async with pool.acquire() as broker:
            transaction = broker.transaction()
            await transaction.start()
            await authority_advisory_lock.acquire_authority_lock(
                broker,
                owner=owner,
            )
            writer = asyncio.create_task(
                repository.ensure_user_identity(
                    uid=uid,
                    email=email,
                    name="Unauthorized Bind",
                    timezone_name="America/Los_Angeles",
                )
            )
            await _wait_for_advisory_waiter(pool, owner)
            assert not writer.done()
            async with pool.acquire() as drift:
                async with drift.transaction():
                    await drift.execute(
                        """
                        UPDATE users
                        SET email = $2,
                            profile_class = 'real'
                        WHERE id = $1
                        """,
                        original_id,
                        f"{uid}-moved@example.invalid",
                    )
                    replacement_id = await drift.fetchval(
                        """
                        INSERT INTO users (
                            email, name, status, profile_class
                        ) VALUES (
                            $1, 'Replacement Email Owner', 'PENDING', 'synthetic'
                        )
                        RETURNING id
                        """,
                        email,
                    )
            await transaction.commit()

        with pytest.raises(
            authority_advisory_lock.AuthorityLockError,
            match="authority_lock_owner_drift",
        ):
            await asyncio.wait_for(writer, timeout=5)

        async with pool.acquire() as observer:
            original = await observer.fetchrow(
                """
                SELECT omi_uid, email, name, timezone, profile_class,
                       identities ->> 'omi_uid'
                FROM users
                WHERE id = $1
                """,
                original_id,
            )
            replacement = await observer.fetchrow(
                """
                SELECT omi_uid, email, name, timezone, profile_class,
                       identities ->> 'omi_uid'
                FROM users
                WHERE id = $1
                """,
                replacement_id,
            )
            authority_counts = await observer.fetchrow(
                """
                SELECT
                    (
                        SELECT COUNT(*)
                        FROM ella_managed_cloud_consent_authority
                        WHERE user_id = ANY($1::uuid[])
                    ) AS consent_rows,
                    (
                        SELECT COUNT(*)
                        FROM ella_runtime_targets
                        WHERE account_user_id = ANY($1::uuid[])
                           OR profile_user_id = ANY($1::uuid[])
                    ) AS target_rows,
                    (
                        SELECT COUNT(*)
                        FROM ella_runtime_bindings
                        WHERE user_id = ANY($1::uuid[])
                    ) AS binding_rows,
                    (
                        SELECT COUNT(*)
                        FROM voice_entitlements
                        WHERE uid = $2
                    ) AS entitlement_rows
                """,
                [original_id, replacement_id],
                uid,
            )

        assert tuple(original.values()) == (
            None,
            f"{uid}-moved@example.invalid",
            "Original Email Owner",
            "UTC",
            "real",
            None,
        )
        assert tuple(replacement.values()) == (
            None,
            email,
            "Replacement Email Owner",
            "UTC",
            "synthetic",
            None,
        )
        assert tuple(authority_counts.values()) == (0, 0, 0, 0)

        async with pool.acquire() as probe:
            async with probe.transaction():
                proof = await asyncio.wait_for(
                    authority_advisory_lock.acquire_authority_lock(
                        probe,
                        owner=owner,
                    ),
                    timeout=2,
                )
                await authority_advisory_lock.require_authority_lock(
                    probe,
                    proof,
                    owner=owner,
                )

    asyncio.run(_run_with_database(scenario))


def test_new_identity_owner_collision_after_lookup_fails_closed():
    async def scenario(pool):
        uid = "synthetic-new-owner-collision"
        email = f"{uid}@example.invalid"
        owner = authority_advisory_lock.provisional_identity_owner(uid)
        repository = EllaProvisioningRepository(pool)
        async with pool.acquire() as broker:
            transaction = broker.transaction()
            await transaction.start()
            await authority_advisory_lock.acquire_authority_lock(
                broker,
                owner=owner,
            )
            writer = asyncio.create_task(
                repository.ensure_user_identity(
                    uid=uid,
                    email=email,
                    name="Provisional Owner",
                    timezone_name="UTC",
                )
            )
            await _wait_for_advisory_waiter(pool, owner)
            async with pool.acquire() as drift:
                replacement_id = await drift.fetchval(
                    """
                    INSERT INTO users (
                        omi_uid, email, name, status, profile_class
                    ) VALUES (
                        $1, $2, 'Replacement Owner', 'PENDING', 'synthetic'
                    )
                    RETURNING id
                    """,
                    uid,
                    email,
                )
            await transaction.commit()

        with pytest.raises(
            authority_advisory_lock.AuthorityLockError,
            match="authority_lock_owner_drift",
        ):
            await asyncio.wait_for(writer, timeout=5)
        async with pool.acquire() as observer:
            rows = await observer.fetch(
                """
                SELECT id, name, status
                FROM users
                WHERE omi_uid = $1 OR id = $2
                ORDER BY id
                """,
                uid,
                owner.account_id,
            )
        assert [dict(row) for row in rows] == [
            {
                "id": replacement_id,
                "name": "Replacement Owner",
                "status": "PENDING",
            }
        ]

    asyncio.run(_run_with_database(scenario))
