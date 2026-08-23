import asyncio
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import asyncpg
import httpx
import pytest
from fastapi import FastAPI

from database import authority_advisory_lock, managed_cloud_consent, voice_canary
from database.ella_provisioning import EllaProvisioningRepository
from database.runtime_targets import RuntimeTargetLineage
from ella.routers import guardian
from ella.services.provisioning import current_self_hosted_runtime_lineage
from utils.ella import exact_firebase_auth

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
    guardian_mode TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE agent_clusters (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    agents JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'ACTIVE'
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


def test_guardian_mode_get_returns_only_exact_case_sensitive_firebase_subject():
    async def scenario(pool):
        await pool.execute("""
            INSERT INTO users (omi_uid, guardian_mode)
            VALUES ('CaseUID', 'EMERGENCY_ONLY'), ('caseuid', 'ACTIVE_SUPPORT')
            """)
        previous_pool = guardian._pool
        guardian._pool = pool
        try:
            upper = await guardian.get_guardian_mode(authenticated_uid="CaseUID")
            lower = await guardian.get_guardian_mode(authenticated_uid="caseuid")
        finally:
            guardian._pool = previous_pool

        assert upper["currentMode"] == "EMERGENCY_ONLY"
        assert lower["currentMode"] == "ACTIVE_SUPPORT"

    asyncio.run(_run_with_database(scenario))


def test_mounted_guardian_alert_history_isolates_case_distinct_firebase_subjects(monkeypatch):
    async def scenario(pool):
        await pool.execute("""
            CREATE TABLE guardian_queue (
                id TEXT PRIMARY KEY,
                uid TEXT NOT NULL,
                url TEXT NOT NULL DEFAULT '',
                priority TEXT NOT NULL,
                message TEXT,
                trigger_type TEXT,
                metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                consumed_at TIMESTAMPTZ
            );

            CREATE TABLE guardian_pipeline_events (
                id BIGSERIAL PRIMARY KEY,
                trace_id TEXT NOT NULL,
                uid TEXT NOT NULL,
                stage TEXT NOT NULL,
                status TEXT NOT NULL,
                latency_ms INTEGER,
                metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE guardian_delivery_log (
                id BIGSERIAL PRIMARY KEY,
                trace_id TEXT NOT NULL,
                uid TEXT NOT NULL,
                channel TEXT NOT NULL,
                target TEXT NOT NULL,
                caregiver_id TEXT,
                status TEXT NOT NULL,
                error_message TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            INSERT INTO users (omi_uid, timezone)
            VALUES ('CaseUID', 'UTC'), ('caseuid', 'America/Los_Angeles');

            INSERT INTO guardian_queue (
                id, uid, priority, message, trigger_type, metadata, created_at
            ) VALUES
                (
                    'upper-private', 'CaseUID', 'urgent', 'UPPER_QUEUE_PRIVATE', 'safety',
                    '{"trace_id":"owner-local-trace","private":"UPPER_QUEUE_METADATA"}',
                    '2026-08-03T12:01:00Z'
                ),
                (
                    'lower-private', 'caseuid', 'normal', 'LOWER_QUEUE_PRIVATE', 'safety',
                    '{"trace_id":"owner-local-trace","private":"LOWER_QUEUE_METADATA"}',
                    '2026-08-03T12:00:00Z'
                );

            INSERT INTO guardian_pipeline_events (
                trace_id, uid, stage, status, metadata, created_at
            ) VALUES
                (
                    'owner-local-trace', 'CaseUID', 'upper-private-event', 'success',
                    '{"private":"UPPER_EVENT_PRIVATE"}', '2026-08-03T12:01:10Z'
                ),
                (
                    'owner-local-trace', 'caseuid', 'lower-private-event', 'success',
                    '{"private":"LOWER_EVENT_PRIVATE"}', '2026-08-03T12:00:10Z'
                );

            INSERT INTO guardian_delivery_log (
                trace_id, uid, channel, target, status, error_message, created_at, updated_at
            ) VALUES
                (
                    'owner-local-trace', 'CaseUID', 'email', 'upper-private-target', 'sent',
                    'UPPER_DELIVERY_PRIVATE', '2026-08-03T12:01:20Z', '2026-08-03T12:01:20Z'
                ),
                (
                    'owner-local-trace', 'caseuid', 'imessage', 'lower-private-target', 'sent',
                    'LOWER_DELIVERY_PRIVATE', '2026-08-03T12:00:20Z', '2026-08-03T12:00:20Z'
                );
            """)

        before = {
            table: [tuple(row.values()) for row in await pool.fetch(f"SELECT * FROM {table} ORDER BY id")]
            for table in ("guardian_queue", "guardian_pipeline_events", "guardian_delivery_log")
        }

        def verify_token(token):
            if token == "valid-lower":
                return {"uid": "caseuid"}
            if token == "valid-upper":
                return {"uid": "CaseUID"}
            raise ValueError("invalid bearer")

        monkeypatch.setattr(exact_firebase_auth.firebase_auth, "verify_id_token", verify_token)
        previous_pool = guardian._pool
        guardian._pool = pool
        app = FastAPI()
        app.include_router(guardian.alerts_router)
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                lower_response = await client.get(
                    "/v1/ella/guardian-alerts?limit=50&uid=CaseUID",
                    headers={"Authorization": "Bearer valid-lower"},
                )
                upper_response = await client.get(
                    "/v1/ella/guardian-alerts?limit=50",
                    headers={"Authorization": "Bearer valid-upper"},
                )
        finally:
            guardian._pool = previous_pool

        assert lower_response.status_code == 200
        lower = lower_response.json()
        assert lower["uid"] == "caseuid"
        assert [alert["queue_item_id"] for alert in lower["alerts"]] == ["lower-private"]
        assert [event["stage"] for event in lower["alerts"][0]["events"]] == ["lower-private-event"]
        assert [delivery["target"] for delivery in lower["alerts"][0]["deliveries"]] == ["lower-private-target"]
        lower_serialized = str(lower)
        assert "LOWER_QUEUE_PRIVATE" in lower_serialized
        for upper_private in (
            "upper-private",
            "UPPER_QUEUE_PRIVATE",
            "UPPER_QUEUE_METADATA",
            "upper-private-event",
            "UPPER_EVENT_PRIVATE",
            "upper-private-target",
            "UPPER_DELIVERY_PRIVATE",
        ):
            assert upper_private not in lower_serialized

        assert upper_response.status_code == 200
        upper = upper_response.json()
        assert upper["uid"] == "CaseUID"
        assert [alert["queue_item_id"] for alert in upper["alerts"]] == ["upper-private"]
        assert [event["stage"] for event in upper["alerts"][0]["events"]] == ["upper-private-event"]
        assert [delivery["target"] for delivery in upper["alerts"][0]["deliveries"]] == ["upper-private-target"]

        after = {
            table: [tuple(row.values()) for row in await pool.fetch(f"SELECT * FROM {table} ORDER BY id")]
            for table in ("guardian_queue", "guardian_pipeline_events", "guardian_delivery_log")
        }
        assert after == before

    asyncio.run(_run_with_database(scenario))


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


def _reissued_managed_cloud_grant(
    grant: managed_cloud_consent.ManagedCloudGrant,
    *,
    receipt_id: str,
) -> managed_cloud_consent.ManagedCloudGrant:
    return managed_cloud_consent.ManagedCloudGrant(
        account_uid=grant.account_uid,
        profile_uid=grant.profile_uid,
        consent_receipt_id=receipt_id,
        profile_binding_id=grant.profile_binding_id,
        policy_version=grant.policy_version,
        processor_set_hash=grant.processor_set_hash,
        scope_version=grant.scope_version,
        scope_hash=grant.scope_hash,
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
            await conn.execute("""
                CREATE FUNCTION hold_authority_revoke() RETURNS trigger AS $$
                BEGIN
                    PERFORM pg_sleep(0.6);
                    RETURN NEW;
                END
                $$ LANGUAGE plpgsql
                """)
            await conn.execute("""
                CREATE TRIGGER hold_authority_revoke
                BEFORE UPDATE ON ella_managed_cloud_consent_authority
                FOR EACH ROW EXECUTE FUNCTION hold_authority_revoke()
                """)

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

    if name in {"identity_create", "identity_update", "identity_bind", "user_activate", "guardian_mode"}:
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
                    ) VALUES ($1, 'Synthetic Before', 'PENDING', 'synthetic')
                    RETURNING id
                    """,
                    email,
                )
            else:
                user_id = await conn.fetchval(
                    """
                    INSERT INTO users (
                        omi_uid, email, name, status, profile_class
                    ) VALUES ($1, $2, 'Synthetic Before', 'PENDING', 'synthetic')
                    RETURNING id
                    """,
                    uid,
                    email,
                )
                if name == "guardian_mode":
                    await conn.execute(
                        "UPDATE users SET status = 'ACTIVE' WHERE id = $1",
                        user_id,
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

        if name == "guardian_mode":

            async def guardian_mode_snapshot():
                return await pool.fetchval(
                    "SELECT guardian_mode FROM users WHERE id = $1",
                    user_id,
                )

            return _WriterCase(
                owner=owner,
                writer=lambda: repository.update_guardian_mode(uid, "ACTIVE_SUPPORT"),
                snapshot=guardian_mode_snapshot,
                expected_before=None,
                expected_after="ACTIVE_SUPPORT",
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
        "guardian_mode",
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
        "guardian_mode",
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


def test_consent_bootstrap_creates_users_row_and_grant():
    """A fresh UID with no users row completes consent via the bootstrap path,
    creating the deterministic users row inside the advisory-lock transaction,
    and later idempotent re-consent reconciles onto the same row. Without the
    relax flag the strict authority_lock_owner_missing behavior is preserved.
    """

    async def scenario(pool):
        uid = "synthetic-consent-bootstrap-fresh"
        owner = authority_advisory_lock.provisional_identity_owner(uid)

        async with pool.acquire() as conn:
            # Match production: a fresh authority bootstrap must provide the
            # Firebase-verified email and explicitly persist updated_at; neither
            # production column has a default that can mask an incomplete INSERT.
            await conn.execute("ALTER TABLE users ALTER COLUMN email SET NOT NULL")
            await conn.execute("ALTER TABLE users ALTER COLUMN updated_at DROP DEFAULT")
            before = await conn.fetchval(
                "SELECT 1 FROM users WHERE omi_uid = $1",
                uid,
            )
        assert before is None

        result = await managed_cloud_consent.synchronize_grant(
            grant=_managed_cloud_grant(uid, revision="one"),
            allow_fresh_uid_bootstrap=True,
            bootstrap_email="fresh-consent@example.invalid",
        )
        assert result["user_id"] == owner.account_id

        async with pool.acquire() as observer:
            row = await observer.fetchrow(
                """
                SELECT id, omi_uid, email, name, timezone, status, identities,
                       updated_at
                FROM users
                WHERE omi_uid = $1
                """,
                uid,
            )
            decision = await observer.fetchval(
                """
                SELECT decision
                FROM ella_managed_cloud_consent_authority
                WHERE user_id = $1
                """,
                owner.account_id,
            )
            receipt_ref = await observer.fetchval(
                """
                SELECT consent_receipt_ref
                FROM ella_managed_cloud_consent_authority
                WHERE user_id = $1
                """,
                owner.account_id,
            )
        assert row is not None
        identities = json.loads(row["identities"]) if isinstance(row["identities"], str) else row["identities"]
        assert tuple(row.values())[:-2] == (
            owner.account_id,
            uid,
            "fresh-consent@example.invalid",
            "Synthetic User",
            "UTC",
            "PENDING",
        )
        assert identities == {"omi_uid": uid, "email": "fresh-consent@example.invalid"}
        assert row["updated_at"] is not None
        assert decision == "granted"
        assert receipt_ref and receipt_ref.startswith("sha256:")

        # Normal provisioning must reconcile the bootstrap row without changing
        # its deterministic owner or rejecting the same verified identity.
        identity = await EllaProvisioningRepository(pool).ensure_user_identity(
            uid=uid,
            email="fresh-consent@example.invalid",
            name="Fresh Consent User",
            timezone_name="America/Los_Angeles",
        )
        assert identity["id"] == owner.account_id
        assert identity["email"] == "fresh-consent@example.invalid"
        assert identity["name"] == "Fresh Consent User"

        # Idempotent re-consent reconciles onto the same deterministic row.
        result_two = await managed_cloud_consent.synchronize_grant(
            grant=_managed_cloud_grant(uid, revision="two"),
            allow_fresh_uid_bootstrap=True,
            bootstrap_email="fresh-consent@example.invalid",
        )
        assert result_two["user_id"] == owner.account_id
        async with pool.acquire() as observer:
            count = await observer.fetchval(
                "SELECT COUNT(*) FROM users WHERE omi_uid = $1",
                uid,
            )
        assert count == 1

        blank_email_uid = "synthetic-consent-bootstrap-blank-email"
        blank_email_owner = authority_advisory_lock.provisional_identity_owner(blank_email_uid)
        with pytest.raises(
            managed_cloud_consent.ManagedCloudAuthorityUnavailable,
            match="managed_cloud_authority_unavailable",
        ) as blank_email_error:
            await managed_cloud_consent.synchronize_grant(
                grant=_managed_cloud_grant(blank_email_uid),
                allow_fresh_uid_bootstrap=True,
            )
        assert blank_email_error.value.__cause__.code == "authority_lock_bootstrap_email_missing"
        async with pool.acquire() as observer:
            assert await observer.fetchval("SELECT 1 FROM users WHERE omi_uid = $1", blank_email_uid) is None
            assert (
                await observer.fetchval(
                    "SELECT 1 FROM ella_managed_cloud_consent_authority WHERE user_id = $1",
                    blank_email_owner.account_id,
                )
                is None
            )

        # A different fresh UID with the relax flag off fails closed first.
        strict_uid = "synthetic-consent-bootstrap-strict"
        with pytest.raises(
            managed_cloud_consent.ManagedCloudAuthorityUnavailable,
            match="managed_cloud_authority_unavailable",
        ) as raised:
            await managed_cloud_consent.synchronize_grant(
                grant=_managed_cloud_grant(strict_uid),
                allow_fresh_uid_bootstrap=False,
            )
        assert raised.value.__cause__.code == "authority_lock_owner_missing"
        async with pool.acquire() as observer:
            strict_count = await observer.fetchval(
                "SELECT COUNT(*) FROM users WHERE omi_uid = $1",
                strict_uid,
            )
        assert strict_count == 0

    asyncio.run(_run_with_database(scenario))


def test_consent_bootstrap_reconciles_email_owner_and_rejects_conflicting_owners():
    async def scenario(pool):
        uid = "synthetic-consent-email-owner"
        email = "consent-email-owner@example.invalid"
        async with pool.acquire() as conn:
            await conn.execute("ALTER TABLE users ALTER COLUMN email SET NOT NULL")
            email_owner_id = await conn.fetchval(
                """
                INSERT INTO users (email, name, status, profile_class)
                VALUES ($1, 'Legacy Email Owner', 'PENDING', 'real')
                RETURNING id
                """,
                email,
            )

        result = await managed_cloud_consent.synchronize_grant(
            grant=_managed_cloud_grant(uid),
            allow_fresh_uid_bootstrap=True,
            bootstrap_email=email,
        )
        assert result["user_id"] == email_owner_id

        async with pool.acquire() as observer:
            reconciled = await observer.fetchrow(
                """
                SELECT id, omi_uid, email, identities ->> 'omi_uid' AS identity_uid
                FROM users
                WHERE id = $1
                """,
                email_owner_id,
            )
            assert tuple(reconciled.values()) == (email_owner_id, uid, email, uid)
            assert (
                await observer.fetchval(
                    "SELECT COUNT(*) FROM ella_managed_cloud_consent_authority WHERE user_id = $1",
                    email_owner_id,
                )
                == 1
            )

            conflicting_uid = "synthetic-consent-conflicting-uid"
            conflicting_email = "synthetic-consent-conflicting-email@example.invalid"
            uid_owner_id = await observer.fetchval(
                """
                INSERT INTO users (omi_uid, email, name, status, profile_class)
                VALUES ($1, $2, 'UID Owner', 'PENDING', 'real')
                RETURNING id
                """,
                conflicting_uid,
                "uid-owner@example.invalid",
            )
            email_conflict_owner_id = await observer.fetchval(
                """
                INSERT INTO users (email, name, status, profile_class)
                VALUES ($1, 'Email Conflict Owner', 'PENDING', 'real')
                RETURNING id
                """,
                conflicting_email,
            )

        with pytest.raises(
            managed_cloud_consent.ManagedCloudAuthorityUnavailable,
            match="managed_cloud_authority_unavailable",
        ) as conflict:
            await managed_cloud_consent.synchronize_grant(
                grant=_managed_cloud_grant(conflicting_uid),
                allow_fresh_uid_bootstrap=True,
                bootstrap_email=conflicting_email,
            )
        assert conflict.value.__cause__.code == "authority_lock_identity_conflict"

        async with pool.acquire() as observer:
            owners = await observer.fetch(
                """
                SELECT id, omi_uid, email
                FROM users
                WHERE id = ANY($1::uuid[])
                ORDER BY id
                """,
                [uid_owner_id, email_conflict_owner_id],
            )
            assert {row["id"] for row in owners} == {uid_owner_id, email_conflict_owner_id}
            assert next(row for row in owners if row["id"] == uid_owner_id)["omi_uid"] == conflicting_uid
            assert next(row for row in owners if row["id"] == email_conflict_owner_id)["omi_uid"] is None
            assert (
                await observer.fetchval(
                    """
                    SELECT COUNT(*)
                    FROM ella_managed_cloud_consent_authority
                    WHERE user_id = ANY($1::uuid[])
                    """,
                    [uid_owner_id, email_conflict_owner_id],
                )
                == 0
            )

    asyncio.run(_run_with_database(scenario))


def test_consent_bootstrap_serializes_concurrent_email_owner_claims():
    async def scenario(pool):
        uid = "synthetic-consent-email-owner-concurrent"
        email = "consent-email-owner-concurrent@example.invalid"
        async with pool.acquire() as conn:
            await conn.execute("ALTER TABLE users ALTER COLUMN email SET NOT NULL")
            email_owner_id = await conn.fetchval(
                """
                INSERT INTO users (email, name, status, profile_class)
                VALUES ($1, 'Concurrent Email Owner', 'PENDING', 'real')
                RETURNING id
                """,
                email,
            )

        grant = _managed_cloud_grant(uid)
        results = await asyncio.gather(
            managed_cloud_consent.synchronize_grant(
                grant=grant,
                allow_fresh_uid_bootstrap=True,
                bootstrap_email=email,
            ),
            managed_cloud_consent.synchronize_grant(
                grant=grant,
                allow_fresh_uid_bootstrap=True,
                bootstrap_email=email,
            ),
        )
        assert [result["user_id"] for result in results] == [email_owner_id, email_owner_id]

        async with pool.acquire() as observer:
            assert await observer.fetchval("SELECT COUNT(*) FROM users WHERE omi_uid = $1", uid) == 1
            assert (
                await observer.fetchval(
                    "SELECT COUNT(*) FROM ella_managed_cloud_consent_authority WHERE user_id = $1",
                    email_owner_id,
                )
                == 1
            )

    asyncio.run(_run_with_database(scenario))


def test_fresh_managed_terminal_decisions_without_email_leave_zero_usable_authority():
    async def scenario(pool):
        async with pool.acquire() as conn:
            await conn.execute("ALTER TABLE users ALTER COLUMN email SET NOT NULL")

        for decision in ("declined", "revoked"):
            uid = f"synthetic-consent-terminal-{decision}"
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO voice_entitlements (uid, status) VALUES ($1, 'active')",
                    uid,
                )

            result = await managed_cloud_consent.synchronize_denial(
                uid=uid,
                decision=decision,
            )
            assert result == {"decision": decision, "authority_absent": True}

            async with pool.acquire() as observer:
                assert await observer.fetchval("SELECT 1 FROM users WHERE omi_uid = $1", uid) is None
                assert (
                    await observer.fetchval(
                        """
                        SELECT COUNT(*)
                        FROM ella_managed_cloud_consent_authority authority
                        JOIN users app_user ON app_user.id = authority.user_id
                        WHERE app_user.omi_uid = $1
                        """,
                        uid,
                    )
                    == 0
                )
                assert await observer.fetchval("SELECT status FROM voice_entitlements WHERE uid = $1", uid) == "revoked"
                assert await observer.fetchval("SELECT COUNT(*) FROM voice_active_sessions WHERE uid = $1", uid) == 0

    asyncio.run(_run_with_database(scenario))


@pytest.mark.parametrize("decision", ("declined", "revoked"))
def test_fresh_managed_terminal_decision_reconciles_email_owner_before_quarantine(decision):
    async def scenario(pool):
        uid = f"synthetic-email-owner-terminal-{decision}"
        email = f"email-owner-terminal-{decision}@example.invalid"
        async with pool.acquire() as conn:
            await conn.execute("ALTER TABLE users ALTER COLUMN email SET NOT NULL")
            user_id = await conn.fetchval(
                """
                INSERT INTO users (email, name, status, profile_class)
                VALUES ($1, 'Legacy Email Owner', 'PENDING', 'real')
                RETURNING id
                """,
                email,
            )
            await conn.execute(
                "INSERT INTO voice_entitlements (uid, status) VALUES ($1, 'active')",
                uid,
            )

        result = await managed_cloud_consent.synchronize_denial(
            uid=uid,
            decision=decision,
            verified_email=email,
        )
        assert result["user_id"] == user_id
        assert result["decision"] == decision

        async with pool.acquire() as observer:
            identity = await observer.fetchrow(
                "SELECT omi_uid, email FROM users WHERE id = $1",
                user_id,
            )
            assert tuple(identity.values()) == (uid, email)
            assert (
                await observer.fetchval(
                    "SELECT decision FROM ella_managed_cloud_consent_authority WHERE user_id = $1",
                    user_id,
                )
                == decision
            )
            assert await observer.fetchval("SELECT status FROM voice_entitlements WHERE uid = $1", uid) == "revoked"
            assert await observer.fetchval("SELECT COUNT(*) FROM voice_active_sessions WHERE uid = $1", uid) == 0

    asyncio.run(_run_with_database(scenario))


def test_fresh_regrant_is_idempotent_and_recovers_only_exact_quarantine():
    async def scenario(pool):
        uid = "synthetic-fresh-regrant-recovery"
        initial_grant = _managed_cloud_grant(uid)
        initial = await managed_cloud_consent.synchronize_grant(
            grant=initial_grant,
            allow_fresh_uid_bootstrap=True,
            bootstrap_email="fresh-regrant@example.invalid",
        )
        user_id = initial["user_id"]
        job_id = uuid.uuid4()
        binding_id = uuid.uuid4()
        repository = EllaProvisioningRepository(pool)
        assert await repository.seed_voice_entitlement_if_absent(uid=uid) is True

        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO ella_provisioning_jobs (
                    id, user_id, target_schema_version, request_payload_hash,
                    state, stage, retryable
                ) VALUES (
                    $1, $2, 'hermes-user-v1', 'synthetic',
                    'provisioning', 'smoke_passed', TRUE
                )
                """,
                job_id,
                user_id,
            )
            await conn.execute(
                """
                INSERT INTO ella_runtime_bindings (
                    id, user_id, account_user_id, profile_user_id,
                    role, provider, profile_name, agent_id,
                    template_version, model_policy_version, voice_policy_version,
                    health_state, health_receipt, runtime_target_mode,
                    status, active
                ) VALUES (
                    $1, $2, $2, $2,
                    'user', 'hermes', 'fresh-regrant-profile', 'fresh-regrant-agent',
                    'hermes-user-v1', 'model-policy-v1', 'voice-policy-v1',
                    'healthy', '{}'::jsonb, 'hermes-chat',
                    'shadow', FALSE
                )
                """,
                binding_id,
                user_id,
            )
            initial_epoch = await conn.fetchval(
                """
                SELECT authority_epoch
                FROM ella_managed_cloud_consent_authority
                WHERE user_id = $1
                """,
                user_id,
            )
            initial_revision = await conn.fetchval(
                """
                SELECT revision
                FROM ella_managed_cloud_consent_authority
                WHERE user_id = $1
                """,
                user_id,
            )

        exact_repeat = await managed_cloud_consent.synchronize_grant(
            grant=initial_grant,
            allow_fresh_uid_bootstrap=True,
            bootstrap_email="fresh-regrant@example.invalid",
        )
        assert exact_repeat["authority_epoch"] == initial_epoch
        assert exact_repeat["revision"] == initial_revision

        repeated_grant = _reissued_managed_cloud_grant(
            initial_grant,
            receipt_id="synthetic-receipt-reaffirmed",
        )
        repeated = await managed_cloud_consent.synchronize_grant(
            grant=repeated_grant,
            allow_fresh_uid_bootstrap=True,
            bootstrap_email="fresh-regrant@example.invalid",
        )
        assert repeated["authority_epoch"] == initial_epoch

        async with pool.acquire() as observer:
            unchanged = await observer.fetchrow(
                """
                SELECT job.state, job.stage, binding.status,
                       binding.health_state, binding.active,
                       entitlement.status AS entitlement_status
                FROM ella_provisioning_jobs job
                JOIN ella_runtime_bindings binding
                  ON binding.user_id = job.user_id
                JOIN voice_entitlements entitlement
                  ON entitlement.uid = $2
                WHERE job.id = $1
                """,
                job_id,
                uid,
            )
        assert tuple(unchanged.values()) == (
            "provisioning",
            "smoke_passed",
            "shadow",
            "healthy",
            False,
            "active",
        )

        # Reproduce the exact production state left by the old receipt-change
        # path. The next valid same-contract grant may rearm this state, but it
        # must not activate either the binding or voice row by itself.
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE ella_provisioning_jobs
                SET state = 'blocked',
                    stage = 'runtime_ready',
                    retryable = FALSE,
                    error_code = 'invitation_authority_revoked',
                    error_detail = '{"content_free":true,"reason":"managed_cloud_consent_grant_changed"}'::jsonb
                WHERE id = $1
                """,
                job_id,
            )
            await conn.execute(
                """
                UPDATE ella_runtime_bindings
                SET status = 'disabled',
                    active = FALSE,
                    health_state = 'unhealthy',
                    quarantine_reason = 'managed_cloud_consent_grant_changed',
                    disabled_at = CURRENT_TIMESTAMP,
                    runtime_target_mode = NULL
                WHERE id = $1
                """,
                binding_id,
            )
            await conn.execute(
                """
                UPDATE voice_entitlements
                SET status = 'revoked',
                    operator_note = 'Owner-authorized Plato Grok voice restore for ella-ai#1171 on 2026-07-31',
                    revision = revision + 1
                WHERE uid = $1
                """,
                uid,
            )

        nonmatching_grant = _reissued_managed_cloud_grant(
            initial_grant,
            receipt_id="synthetic-receipt-nonmatching-quarantine",
        )
        nonmatching = await managed_cloud_consent.synchronize_grant(
            grant=nonmatching_grant,
            allow_fresh_uid_bootstrap=True,
            bootstrap_email="fresh-regrant@example.invalid",
        )
        assert nonmatching["authority_epoch"] == initial_epoch
        async with pool.acquire() as observer:
            still_blocked = await observer.fetchrow(
                """
                SELECT job.state, job.error_code, binding.status,
                       binding.runtime_target_mode,
                       entitlement.status AS entitlement_status
                FROM ella_provisioning_jobs job
                JOIN ella_runtime_bindings binding
                  ON binding.user_id = job.user_id
                JOIN voice_entitlements entitlement
                  ON entitlement.uid = $2
                WHERE job.id = $1
                """,
                job_id,
                uid,
            )
        assert tuple(still_blocked.values()) == (
            "blocked",
            "invitation_authority_revoked",
            "disabled",
            None,
            "revoked",
        )

        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE ella_runtime_bindings
                SET runtime_target_mode = 'hermes-chat'
                WHERE id = $1
                """,
                binding_id,
            )

        recovery_grant = _reissued_managed_cloud_grant(
            initial_grant,
            receipt_id="synthetic-receipt-recovery",
        )
        recovered = await managed_cloud_consent.synchronize_grant(
            grant=recovery_grant,
            allow_fresh_uid_bootstrap=True,
            bootstrap_email="fresh-regrant@example.invalid",
        )
        assert recovered["authority_epoch"] == initial_epoch

        async with pool.acquire() as observer:
            pending = await observer.fetchrow(
                """
                SELECT job.state, job.stage, job.retryable, job.error_code,
                       binding.status, binding.health_state, binding.active,
                       binding.quarantine_reason,
                       entitlement.status AS entitlement_status
                FROM ella_provisioning_jobs job
                JOIN ella_runtime_bindings binding
                  ON binding.user_id = job.user_id
                JOIN voice_entitlements entitlement
                  ON entitlement.uid = $2
                WHERE job.id = $1
                """,
                job_id,
                uid,
            )
        assert tuple(pending.values()) == (
            "pending",
            "identity_ready",
            True,
            None,
            "shadow",
            "pending",
            False,
            None,
            "revoked",
        )

        staged = await repository.stage_runtime_binding(
            uid=uid,
            binding={
                "binding_id": str(binding_id),
                "provider": "hermes",
                "profile_name": "fresh-regrant-profile",
                "agent_id": "fresh-regrant-agent",
                "template_version": "hermes-user-v1",
                "model_policy_version": "model-policy-v1",
                "voice_policy_version": "voice-policy-v1",
                "health_state": "healthy",
                "health_receipt": {"content_free": True},
                "runtime_target_mode": "hermes-chat",
            },
        )
        assert staged["status"] == "shadow"
        smoke_passed = await repository.update_job(
            job_id=str(job_id),
            state="provisioning",
            stage="smoke_passed",
            retryable=True,
            receipt={"type": "runtime_smoke_passed", "content_free": True},
        )
        assert smoke_passed["stage"] == "smoke_passed"
        assert await repository.seed_voice_entitlement_if_absent(uid=uid) is True
        activated = await repository.activate_runtime_binding(
            uid=uid,
            provider="hermes",
            require_invitation_target=False,
        )
        assert activated["id"] == binding_id
        assert activated["status"] == "active"
        assert activated["active"] is True
        async with pool.acquire() as observer:
            assert await observer.fetchval("SELECT status FROM users WHERE id = $1", user_id) == "ACTIVE"
            assert await observer.fetchval("SELECT status FROM voice_entitlements WHERE uid = $1", uid) == "active"

        # Returning pre-invitation accounts can retain a valid legacy Hermes
        # cluster after a consent-change quarantine. Recovery must still require
        # the exact current lineage and exact stale quarantine shape.
        current_lineage = current_self_hosted_runtime_lineage()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO agent_clusters (user_id, agents, status)
                VALUES ($1, '{"userAgentId":"retained-user-agent"}'::jsonb, 'ACTIVE')
                """,
                user_id,
            )
            await conn.execute(
                """
                UPDATE ella_provisioning_jobs
                SET state = 'blocked',
                    stage = 'runtime_ready',
                    retryable = FALSE,
                    error_code = 'invitation_authority_revoked',
                    error_detail = '{"content_free":true,"reason":"managed_cloud_consent_grant_changed"}'::jsonb
                WHERE id = $1
                """,
                job_id,
            )
            await conn.execute(
                """
                UPDATE ella_runtime_bindings
                SET status = 'disabled',
                    active = FALSE,
                    health_state = 'unhealthy',
                    quarantine_reason = 'managed_cloud_consent_grant_changed'
                WHERE id = $1
                """,
                binding_id,
            )
            await conn.execute(
                """
                UPDATE voice_entitlements
                SET status = 'revoked', revision = revision + 1
                WHERE uid = $1
                """,
                uid,
            )

        # The retained cluster alone is insufficient while consent authority is
        # stale. No entitlement mutation is permitted.
        assert (
            await repository.seed_voice_entitlement_if_absent(
                uid=uid,
                retained_authority_lineage=current_lineage,
            )
            is False
        )
        async with pool.acquire() as observer:
            assert await observer.fetchval("SELECT status FROM voice_entitlements WHERE uid = $1", uid) == "revoked"

        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE ella_managed_cloud_consent_authority
                SET policy_version = $2,
                    processor_set_hash = $3,
                    scope_version = $4,
                    scope_hash = $5
                WHERE user_id = $1
                """,
                user_id,
                current_lineage.policy_version,
                current_lineage.processor_set_hash,
                current_lineage.scope_version,
                current_lineage.scope_hash,
            )

        assert (
            await repository.seed_voice_entitlement_if_absent(
                uid=uid,
                retained_authority_lineage=current_lineage,
            )
            is True
        )
        async with pool.acquire() as observer:
            recovered_entitlement = await observer.fetchrow(
                "SELECT status, revision FROM voice_entitlements WHERE uid = $1",
                uid,
            )
            recovery_receipts = await observer.fetchval(
                "SELECT receipts FROM ella_provisioning_jobs WHERE id = $1",
                job_id,
            )
        if isinstance(recovery_receipts, str):
            recovery_receipts = json.loads(recovery_receipts)
        assert dict(recovered_entitlement) == {"status": "active", "revision": 5}
        assert {"type": "retained_entitlement_recovered", "content_free": True} in recovery_receipts

        # A later operator revocation is authoritative. The consumed recovery
        # receipt prevents the old quarantine marker from rearming it.
        revoked = await voice_canary.update_entitlement_status(uid=uid, status="revoked")
        assert revoked is not None and revoked["status"] == "revoked"
        assert (
            await repository.seed_voice_entitlement_if_absent(
                uid=uid,
                retained_authority_lineage=current_lineage,
            )
            is False
        )
        async with pool.acquire() as observer:
            final_entitlement = await observer.fetchrow(
                "SELECT status, revision FROM voice_entitlements WHERE uid = $1",
                uid,
            )
        assert dict(final_entitlement) == {"status": "revoked", "revision": 6}

    asyncio.run(_run_with_database(scenario))


def test_seed_voice_entitlement_if_absent_creates_once_without_clobber():
    async def scenario(pool):
        uid = "synthetic-fresh-voice-seed"
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO users (omi_uid, email, profile_class)
                VALUES ($1, $2, 'real')
                """,
                uid,
                "fresh-voice-seed@example.invalid",
            )

        repository = EllaProvisioningRepository(pool)
        assert await repository.seed_voice_entitlement_if_absent(uid=uid) is True

        async with pool.acquire() as observer:
            seeded = await observer.fetchrow(
                """
                SELECT status, plan, daily_limit_s, monthly_limit_s,
                       max_session_s, max_concurrent,
                       provider_allowlist, mode_allowlist, revision
                FROM voice_entitlements
                WHERE uid = $1
                """,
                uid,
            )
        assert dict(seeded) == {
            "status": "active",
            "plan": "canary",
            "daily_limit_s": 2700,
            "monthly_limit_s": 43200,
            "max_session_s": 1200,
            "max_concurrent": 1,
            "provider_allowlist": ["grok-voice"],
            "mode_allowlist": ["v4"],
            "revision": 1,
        }

        assert await repository.seed_voice_entitlement_if_absent(uid=uid) is False
        async with pool.acquire() as observer:
            unchanged = await observer.fetchrow(
                """
                SELECT status, provider_allowlist, mode_allowlist, revision
                FROM voice_entitlements
                WHERE uid = $1
                """,
                uid,
            )
        assert dict(unchanged) == {
            "status": "active",
            "provider_allowlist": ["grok-voice"],
            "mode_allowlist": ["v4"],
            "revision": 1,
        }

        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE voice_entitlements
                SET status = 'revoked', revision = revision + 1
                WHERE uid = $1
                """,
                uid,
            )
        assert await repository.seed_voice_entitlement_if_absent(uid=uid) is False
        async with pool.acquire() as observer:
            unrearmed = await observer.fetchrow(
                """
                SELECT status, provider_allowlist, mode_allowlist, revision
                FROM voice_entitlements
                WHERE uid = $1
                """,
                uid,
            )
        assert dict(unrearmed) == {
            "status": "revoked",
            "provider_allowlist": ["grok-voice"],
            "mode_allowlist": ["v4"],
            "revision": 2,
        }

        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE voice_entitlements
                SET status = 'revoked',
                    operator_note = 'operator-controlled',
                    revision = revision + 1
                WHERE uid = $1
                """,
                uid,
            )
        assert await repository.seed_voice_entitlement_if_absent(uid=uid) is False
        async with pool.acquire() as observer:
            operator_row = await observer.fetchrow(
                """
                SELECT status, operator_note, revision
                FROM voice_entitlements
                WHERE uid = $1
                """,
                uid,
            )
        assert dict(operator_row) == {
            "status": "revoked",
            "operator_note": "operator-controlled",
            "revision": 3,
        }

    asyncio.run(_run_with_database(scenario))


def test_delete_unlinks_users_row_and_consent_authority_freeing_uid():
    """A confirmed delete frees the UID for a fresh bootstrap on relogin.

    After consent bootstrap, unlink_self_owner_account_on_deletion clears the
    consent/entitlement FK dependents and deletes the users row, so the same
    Firebase UID can bootstrap a brand-new account (fresh account on relogin)
    rather than resuming the deleted one — and the re-bootstrap does not collide
    with the tombstoned deterministic id.
    """

    async def scenario(pool):
        uid = "synthetic-delete-unlink-fresh"
        owner = authority_advisory_lock.provisional_identity_owner(uid)
        await managed_cloud_consent.synchronize_grant(
            grant=_managed_cloud_grant(uid, revision="one"),
            allow_fresh_uid_bootstrap=True,
            bootstrap_email="fresh-delete@example.invalid",
        )

        async with pool.acquire() as observer:
            assert (
                await observer.fetchval(
                    "SELECT COUNT(*) FROM users WHERE omi_uid = $1",
                    uid,
                )
                == 1
            )
            assert (
                await observer.fetchval(
                    "SELECT COUNT(*) FROM ella_managed_cloud_consent_authority WHERE user_id = $1",
                    owner.account_id,
                )
                == 1
            )

        # Unlink the account.
        await managed_cloud_consent.unlink_self_owner_account_on_deletion(uid=uid)

        async with pool.acquire() as observer:
            assert (
                await observer.fetchval(
                    "SELECT COUNT(*) FROM users WHERE omi_uid = $1",
                    uid,
                )
                == 0
            )
            assert (
                await observer.fetchval(
                    "SELECT COUNT(*) FROM ella_managed_cloud_consent_authority WHERE user_id = $1",
                    owner.account_id,
                )
                == 0
            )

        # A relogin re-bootstraps a fresh account collision-free.
        result = await managed_cloud_consent.synchronize_grant(
            grant=_managed_cloud_grant(uid, revision="two"),
            allow_fresh_uid_bootstrap=True,
            bootstrap_email="fresh-delete@example.invalid",
        )
        assert result["user_id"] == owner.account_id
        async with pool.acquire() as observer:
            assert (
                await observer.fetchval(
                    "SELECT COUNT(*) FROM users WHERE omi_uid = $1",
                    uid,
                )
                == 1
            )
            assert (
                await observer.fetchval(
                    "SELECT COUNT(*) FROM ella_managed_cloud_consent_authority WHERE user_id = $1",
                    owner.account_id,
                )
                == 1
            )

        # Deleting again is a no-op (no server row), not an error.
        await managed_cloud_consent.unlink_self_owner_account_on_deletion(uid=uid)
        async with pool.acquire() as observer:
            assert (
                await observer.fetchval(
                    "SELECT COUNT(*) FROM users WHERE omi_uid = $1",
                    uid,
                )
                == 0
            )

    asyncio.run(_run_with_database(scenario))
