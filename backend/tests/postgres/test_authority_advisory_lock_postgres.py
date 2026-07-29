import asyncio
import os
import uuid
from pathlib import Path

import asyncpg
import pytest

from database import authority_advisory_lock, managed_cloud_consent, voice_canary

TEST_DSN = os.getenv("ELLA_TEST_POSTGRES_DSN", "").strip()
MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"

pytestmark = pytest.mark.skipif(
    not TEST_DSN,
    reason="ELLA_TEST_POSTGRES_DSN is required for authority-lock PostgreSQL tests",
)

BASE_PROVISIONING_SCHEMA = """
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    omi_uid TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL DEFAULT 'Synthetic User',
    status TEXT NOT NULL DEFAULT 'ACTIVE',
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
