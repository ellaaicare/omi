import asyncio
import os
import uuid
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
    omi_uid TEXT NOT NULL UNIQUE
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
            await conn.execute((MIGRATIONS / "008_create_voice_canary_controls.sql").read_text(encoding="utf-8"))
            await conn.execute((MIGRATIONS / "009_create_hermes_cloud_runtime_pool.sql").read_text(encoding="utf-8"))
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
                "INSERT INTO users (omi_uid) VALUES ($1) RETURNING id",
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
            prompt_artifact_receipt={"status": "approved", "content_free": True},
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
