import asyncio
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import pytest

from database import voice_canary
from database.ella_provisioning import (
    EllaProvisioningRepository,
    RuntimePoolClaimError,
)

TEST_DSN = os.getenv("ELLA_TEST_POSTGRES_DSN", "").strip()
MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"

pytestmark = pytest.mark.skipif(
    not TEST_DSN,
    reason="ELLA_TEST_POSTGRES_DSN is required for Hermes Cloud PostgreSQL tests",
)

BASE_PROVISIONING_SCHEMA = """
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    omi_uid TEXT NOT NULL UNIQUE,
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


def _prompt_receipt(*, model: str = "gpt-5.6-terra") -> dict:
    return {
        "schema_version": "ella-hermes-cloud-approval-v1",
        "prompt_pack_version": "prompt-v1",
        "model_policy_version": "model-policy-v1",
        "expected_model": model,
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


async def _run_with_database(scenario):
    schema = f"hermes_claim_{uuid.uuid4().hex}"
    admin = await asyncpg.connect(TEST_DSN)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(
        TEST_DSN,
        min_size=1,
        max_size=3,
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
                "012_create_account_profile_runtime_targets.sql",
            ):
                await conn.execute((MIGRATIONS / name).read_text(encoding="utf-8"))
            assert await conn.fetchval("""
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name = 'users'
                      AND column_name = 'profile_class'
                      AND is_nullable = 'NO'
                      AND column_default = '''real''::text'
                )
                """)
            assert await conn.fetchval("""
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_constraint
                    WHERE connamespace = current_schema()::regnamespace
                      AND conname = 'users_profile_class_check'
                )
                """)
        await scenario(pool)
    finally:
        voice_canary._pool = previous_voice_pool
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


def test_revoke_between_admission_and_claim_consumes_no_pool_row():
    async def scenario(pool):
        uid = "synthetic-revoked-before-claim"
        model = "gpt-5.6-terra"
        async with pool.acquire() as conn:
            user_id = await conn.fetchval(
                "INSERT INTO users (omi_uid, profile_class) VALUES ($1, 'synthetic') RETURNING id",
                uid,
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
                    uid, status, provider_allowlist, model_allowlist
                ) VALUES ($1, 'active', ARRAY['hermes_cloud'], ARRAY[$2])
                """,
                uid,
                model,
            )

        repository = EllaProvisioningRepository(pool)
        await repository.register_cloud_pool_binding(
            runtime_instance_id="synthetic-instance-a",
            profile_name="synthetic-profile-a",
            agent_id="hermes-cloud",
            api_base_url_ref="env:ELLA_HERMES_CLOUD_API_URL_SYNTHETIC",
            api_key_ref="env:ELLA_HERMES_CLOUD_API_KEY_SYNTHETIC",
            honcho_api_key_ref="env:ELLA_HONCHO_CLOUD_API_KEY_SYNTHETIC",
            template_version="template-v1",
            prompt_pack_version="prompt-v1",
            prompt_artifact_receipt=_prompt_receipt(model=model),
            model_policy_version="model-policy-v1",
            voice_policy_version="voice-policy-v1",
            expected_model=model,
            allowed_tools=[],
            required_capabilities=["responses_api"],
            health_receipt={"status": "ok", "content_free": True},
        )

        admitted = await voice_canary.evaluate_runtime_activation(
            uid=uid,
            provider="hermes_cloud",
            model=model,
        )
        assert admitted.allowed
        admitted_revision = int(admitted.entitlement["revision"])

        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE voice_entitlements SET status = 'revoked' WHERE uid = $1",
                uid,
            )

        with pytest.raises(RuntimePoolClaimError) as error:
            await repository.claim_cloud_pool_binding(
                uid=uid,
                job_id=str(job_id),
                lease_seconds=120,
                admitted_entitlement_revision=admitted_revision,
                provider="hermes_cloud",
                model=model,
                required_profile_class="synthetic",
            )

        assert error.value.code == "runtime_admission_revoked"
        async with pool.acquire() as conn:
            pool_row = dict(await conn.fetchrow("""
                    SELECT status, user_id, claim_job_id, claim_token
                    FROM ella_runtime_bindings
                    WHERE runtime_instance_id = 'synthetic-instance-a'
                    """))
        assert pool_row == {
            "status": "pool_available",
            "user_id": None,
            "claim_job_id": None,
            "claim_token": None,
        }

    asyncio.run(_run_with_database(scenario))


async def _seed_claim(
    pool,
    *,
    uid: str,
    runtime_instance_id: str,
    daily_limit_s: int = 2700,
    profile_class: str = "synthetic",
):
    model = "gpt-5.6-terra"
    async with pool.acquire() as conn:
        user_id = await conn.fetchval(
            "INSERT INTO users (omi_uid, profile_class) VALUES ($1, $2) RETURNING id",
            uid,
            profile_class,
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
                uid, status, provider_allowlist, model_allowlist, daily_limit_s
            ) VALUES ($1, 'active', ARRAY['hermes_cloud'], ARRAY[$2], $3)
            """,
            uid,
            model,
            daily_limit_s,
        )
    repository = EllaProvisioningRepository(pool)
    await repository.register_cloud_pool_binding(
        runtime_instance_id=runtime_instance_id,
        profile_name=f"profile-{runtime_instance_id}",
        agent_id="hermes-cloud",
        api_base_url_ref="env:ELLA_HERMES_CLOUD_API_URL_SYNTHETIC",
        api_key_ref="env:ELLA_HERMES_CLOUD_API_KEY_SYNTHETIC",
        honcho_api_key_ref="env:ELLA_HONCHO_CLOUD_API_KEY_SYNTHETIC",
        template_version="template-v1",
        prompt_pack_version="prompt-v1",
        prompt_artifact_receipt=_prompt_receipt(model=model),
        model_policy_version="model-policy-v1",
        voice_policy_version="voice-policy-v1",
        expected_model=model,
        allowed_tools=[],
        required_capabilities=["responses_api"],
        health_receipt={"status": "ok", "content_free": True},
    )
    admitted = await voice_canary.evaluate_runtime_activation(
        uid=uid,
        provider="hermes_cloud",
        model=model,
    )
    assert admitted.allowed
    return repository, str(job_id), model, int(admitted.entitlement["revision"])


async def _assert_pool_available(pool, runtime_instance_id: str):
    async with pool.acquire() as conn:
        row = dict(
            await conn.fetchrow(
                """
                SELECT status, user_id, claim_job_id, claim_token
                FROM ella_runtime_bindings
                WHERE runtime_instance_id = $1
                """,
                runtime_instance_id,
            )
        )
    assert row == {
        "status": "pool_available",
        "user_id": None,
        "claim_job_id": None,
        "claim_token": None,
    }


def test_real_profile_cannot_consume_synthetic_pool_capacity():
    async def scenario(pool):
        uid = "allowlisted-real-profile"
        instance = "synthetic-instance-profile-class"
        repository, job_id, model, revision = await _seed_claim(
            pool,
            uid=uid,
            runtime_instance_id=instance,
            profile_class="real",
        )

        with pytest.raises(RuntimePoolClaimError) as error:
            await repository.claim_cloud_pool_binding(
                uid=uid,
                job_id=job_id,
                lease_seconds=120,
                admitted_entitlement_revision=revision,
                provider="hermes_cloud",
                model=model,
                required_profile_class="synthetic",
            )

        assert error.value.code == "hermes_cloud_synthetic_profile_required"
        await _assert_pool_available(pool, instance)

    asyncio.run(_run_with_database(scenario))


def test_provider_kill_switch_interleaving_consumes_no_pool_row():
    async def scenario(pool):
        uid = "synthetic-kill-switch-interleaving"
        instance = "synthetic-instance-kill"
        repository, job_id, model, revision = await _seed_claim(
            pool,
            uid=uid,
            runtime_instance_id=instance,
        )

        async with pool.acquire() as writer:
            transaction = writer.transaction()
            await transaction.start()
            committed = False
            try:
                await voice_canary.set_kill_switch_on_connection(
                    writer,
                    scope_type="provider",
                    scope_value="hermes_cloud",
                    enabled=True,
                    reason="synthetic interleaving",
                    updated_by="test",
                )
                claim_task = asyncio.create_task(
                    repository.claim_cloud_pool_binding(
                        uid=uid,
                        job_id=job_id,
                        lease_seconds=120,
                        admitted_entitlement_revision=revision,
                        provider="hermes_cloud",
                        model=model,
                        required_profile_class="synthetic",
                    )
                )
                await asyncio.sleep(0.05)
                assert not claim_task.done()
                await _assert_pool_available(pool, instance)
                await transaction.commit()
                committed = True
            finally:
                if not committed:
                    await transaction.rollback()

        with pytest.raises(RuntimePoolClaimError) as error:
            await claim_task
        assert error.value.code == "runtime_admission_provider_disabled"
        await _assert_pool_available(pool, instance)

    asyncio.run(_run_with_database(scenario))


def test_completed_usage_interleaving_consumes_no_pool_row():
    async def scenario(pool):
        uid = "synthetic-quota-interleaving"
        instance = "synthetic-instance-quota"
        repository, job_id, model, revision = await _seed_claim(
            pool,
            uid=uid,
            runtime_instance_id=instance,
            daily_limit_s=10,
        )

        async with pool.acquire() as writer:
            transaction = writer.transaction()
            await transaction.start()
            committed = False
            try:
                await writer.execute(
                    """
                    INSERT INTO voice_active_sessions (
                        session_id, uid, correlation_id, entitlement_revision,
                        provider, model, mode, accepted_at, last_seen_at
                    ) VALUES (
                        'synthetic-quota-session', $1, 'synthetic-correlation', $2,
                        'hermes_cloud', $3, 'hermes-cloud-chat',
                        NOW() - INTERVAL '20 seconds', NOW()
                    )
                    """,
                    uid,
                    revision,
                    model,
                )
                await voice_canary._complete_session_on_connection(
                    writer,
                    uid=uid,
                    session_id="synthetic-quota-session",
                    input_audio_s=0,
                    output_audio_s=0,
                    connection_s=20,
                    input_audio_bytes=0,
                    output_audio_bytes=0,
                    tool_calls=0,
                    reconnects=0,
                    provider_request_ids=[],
                    termination_reason="completed",
                    normalized_error_code=None,
                    estimated_cost_microusd=0,
                    now=datetime.now(timezone.utc),
                )
                claim_task = asyncio.create_task(
                    repository.claim_cloud_pool_binding(
                        uid=uid,
                        job_id=job_id,
                        lease_seconds=120,
                        admitted_entitlement_revision=revision,
                        provider="hermes_cloud",
                        model=model,
                        required_profile_class="synthetic",
                    )
                )
                await asyncio.sleep(0.05)
                assert not claim_task.done()
                await _assert_pool_available(pool, instance)
                await transaction.commit()
                committed = True
            finally:
                if not committed:
                    await transaction.rollback()

        with pytest.raises(RuntimePoolClaimError) as error:
            await claim_task
        assert error.value.code == "runtime_admission_quota_daily"
        await _assert_pool_available(pool, instance)

    asyncio.run(_run_with_database(scenario))


def test_finalize_ready_cloud_claim_publishes_exact_account_profile_targets():
    async def scenario(pool):
        uid = "synthetic-target-owner"
        instance = "synthetic-instance-target-owner"
        repository, job_id, model, revision = await _seed_claim(
            pool,
            uid=uid,
            runtime_instance_id=instance,
        )
        claimed = await repository.claim_cloud_pool_binding(
            uid=uid,
            job_id=job_id,
            lease_seconds=120,
            admitted_entitlement_revision=revision,
            provider="hermes_cloud",
            model=model,
            required_profile_class="synthetic",
        )
        finalized = await repository.finalize_cloud_pool_claim(
            uid=uid,
            job_id=job_id,
            claim_token=str(claimed["claim_token"]),
            status="internal_canary",
            health_receipt={
                "status": "ok",
                "content_free": True,
                "policy_version": "ai-data-processors-v8",
                "processor_set_hash": "sha256:" + ("1" * 64),
                "scope_version": "managed-cloud-internal-pilot-v2",
                "scope_hash": "sha256:" + ("2" * 64),
                "admission_revision": revision,
            },
        )

        assert finalized["account_user_id"] == finalized["user_id"]
        assert finalized["profile_user_id"] == finalized["user_id"]
        async with pool.acquire() as conn:
            targets = await conn.fetch(
                """
                SELECT mode, candidate_runtime_instance_id, endpoint_ref, credential_ref
                FROM ella_runtime_targets
                WHERE runtime_binding_id = $1
                ORDER BY mode
                """,
                finalized["id"],
            )
        assert [row["mode"] for row in targets] == [
            "hermes-cloud-chat",
            "hermes-cloud-guardian",
            "hermes-cloud-transcript",
            "hermes-cloud-voice",
        ]
        assert {row["candidate_runtime_instance_id"] for row in targets} == {instance}
        assert {row["endpoint_ref"] for row in targets} == {"env:ELLA_HERMES_CLOUD_API_URL_SYNTHETIC"}
        assert {row["credential_ref"] for row in targets} == {"env:ELLA_HERMES_CLOUD_API_KEY_SYNTHETIC"}

    asyncio.run(_run_with_database(scenario))


def test_plato_retained_binding_never_claims_hermes_cloud_pool():
    async def scenario(pool):
        uid = "synthetic-plato-safe"
        instance = "synthetic-instance-plato-safe"
        repository, job_id, model, revision = await _seed_claim(
            pool,
            uid=uid,
            runtime_instance_id=instance,
        )
        async with pool.acquire() as conn:
            user_id = await conn.fetchval("SELECT id FROM users WHERE omi_uid = $1", uid)
            await conn.execute(
                """
                INSERT INTO ella_runtime_bindings (
                    user_id, role, provider, profile_name, agent_id,
                    workspace_root, internal_gateway_url, gateway_port,
                    service_label, credential_ref, honcho_workspace,
                    observed_peer, observer_peer, template_version,
                    model_policy_version, voice_policy_version, health_state,
                    revision, active
                ) VALUES (
                    $1, 'user', 'hermes', 'plato-eval', 'plato',
                    '/Users/ellaai/.hermes/profiles/plato-eval/workspace',
                    'http://127.0.0.1:8701', 8701,
                    'plato-eval', 'env:HERMES_API_SERVER_KEY', 'plato-workspace',
                    'plato', 'ella', 'hermes-user-v1',
                    'frontier-v1', 'ella-voice-v1', 'healthy',
                    1, true
                )
                """,
                user_id,
            )

        claimed = await repository.claim_cloud_pool_binding(
            uid=uid,
            job_id=job_id,
            lease_seconds=120,
            admitted_entitlement_revision=revision,
            provider="hermes_cloud",
            model=model,
            required_profile_class="synthetic",
        )

        assert claimed["provider"] == "hermes_cloud"
        async with pool.acquire() as conn:
            plato = dict(
                await conn.fetchrow(
                    """
                    SELECT provider, profile_name, active, workspace_root, internal_gateway_url,
                           credential_ref, honcho_workspace, observed_peer, observer_peer
                    FROM ella_runtime_bindings
                    WHERE profile_name = 'plato-eval'
                    """
                )
            )
        assert plato == {
            "provider": "hermes",
            "profile_name": "plato-eval",
            "active": True,
            "workspace_root": "/Users/ellaai/.hermes/profiles/plato-eval/workspace",
            "internal_gateway_url": "http://127.0.0.1:8701",
            "credential_ref": "env:HERMES_API_SERVER_KEY",
            "honcho_workspace": "plato-workspace",
            "observed_peer": "plato",
            "observer_peer": "ella",
        }

    asyncio.run(_run_with_database(scenario))
