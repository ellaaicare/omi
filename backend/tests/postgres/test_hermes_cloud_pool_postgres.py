import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg
import pytest

from database import voice_canary
from database.ella_provisioning import (
    EllaProvisioningRepository,
    RuntimePoolClaimError,
)
from database.runtime_targets import RuntimeTargetLineage
from ella.services.hermes_cloud_staged_attestation import (
    GLOBAL_FLAGS_REQUIRED_FALSE,
    UID_SELECTORS,
    StagedAttestationVerifier,
)
from ella.services import runtime_resolver
from ella.services.hermes_cloud_policy import CurrentCloudAuthority

TEST_DSN = os.getenv("ELLA_TEST_POSTGRES_DSN", "").strip()
MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
LINEAGE = RuntimeTargetLineage(
    policy_version="ai-data-processors-v8",
    processor_set_hash="sha256:" + ("1" * 64),
    scope_version="managed-cloud-internal-pilot-v2",
    scope_hash="sha256:" + ("2" * 64),
)

pytestmark = pytest.mark.skipif(
    not TEST_DSN,
    reason="ELLA_TEST_POSTGRES_DSN is required for Hermes Cloud PostgreSQL tests",
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


def _binding_contract() -> dict:
    return json.loads((FIXTURES / "hermes_binding_envelope_v1.json").read_text(encoding="utf-8"))


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
                "011_create_invitation_redemption.sql",
                "012_create_account_profile_runtime_targets.sql",
                "013_create_managed_cloud_consent_authority.sql",
            ):
                await conn.execute((MIGRATIONS / name).read_text(encoding="utf-8"))
            assert await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name = 'users'
                      AND column_name = 'profile_class'
                      AND is_nullable = 'NO'
                      AND column_default = '''real''::text'
                )
                """
            )
            assert await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_constraint
                    WHERE connamespace = current_schema()::regnamespace
                      AND conname = 'users_profile_class_check'
                )
                """
            )
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
            honcho_api_key_ref=None,
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
            pool_row = dict(
                await conn.fetchrow(
                    """
                    SELECT status, user_id, claim_job_id, claim_token
                    FROM ella_runtime_bindings
                    WHERE runtime_instance_id = 'synthetic-instance-a'
                    """
                )
            )
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
    allowed_tools: list[str] | None = None,
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
        consent_authority_epoch = await conn.fetchval(
            """
            INSERT INTO ella_managed_cloud_consent_authority (
                user_id, decision, consent_receipt_ref, profile_binding_id,
                policy_version, processor_set_hash, scope_version, scope_hash
            ) VALUES (
                $1, 'granted', $2, $3, $4, $5, $6, $7
            )
            RETURNING authority_epoch
            """,
            user_id,
            "sha256:" + ("f" * 64),
            uid,
            LINEAGE.policy_version,
            LINEAGE.processor_set_hash,
            LINEAGE.scope_version,
            LINEAGE.scope_hash,
        )
        await conn.execute(
            """
            INSERT INTO voice_entitlements (
                uid, status, provider_allowlist, model_allowlist, mode_allowlist,
                daily_limit_s, consent_policy_version,
                consent_processor_set_hash, consent_scope_version,
                consent_scope_hash, consent_authority_epoch
            ) VALUES (
                $1, 'active', ARRAY['hermes_cloud'], ARRAY[$2],
                ARRAY[
                    'hermes-cloud-chat', 'hermes-cloud-voice',
                    'hermes-cloud-transcript', 'hermes-cloud-guardian',
                    'hermes-cloud-photon'
                ],
                $3, $4, $5, $6, $7, $8
            )
            """,
            uid,
            model,
            daily_limit_s,
            LINEAGE.policy_version,
            LINEAGE.processor_set_hash,
            LINEAGE.scope_version,
            LINEAGE.scope_hash,
            consent_authority_epoch,
        )
    repository = EllaProvisioningRepository(pool)
    await repository.register_cloud_pool_binding(
        runtime_instance_id=runtime_instance_id,
        profile_name=f"profile-{runtime_instance_id}",
        agent_id="hermes-cloud",
        api_base_url_ref="env:ELLA_HERMES_CLOUD_API_URL_SYNTHETIC",
        api_key_ref="env:ELLA_HERMES_CLOUD_API_KEY_SYNTHETIC",
        honcho_api_key_ref=None,
        template_version="template-v1",
        prompt_pack_version="prompt-v1",
        prompt_artifact_receipt=_prompt_receipt(model=model),
        model_policy_version="model-policy-v1",
        voice_policy_version="voice-policy-v1",
        expected_model=model,
        allowed_tools=allowed_tools or [],
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


def test_real_postgres_claim_revalidates_jsonb_string_pins(tmp_path, monkeypatch):
    uid = "synthetic-staged-jsonb-revalidation"
    instance = "synthetic-instance-staged-jsonb"
    model = "gpt-5.6-terra"
    now = datetime(2026, 7, 30, 1, 0, tzinfo=timezone.utc)
    root = tmp_path / "attestations"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    receipt_path = root / "staged-jsonb.receipt.json"
    verifier = StagedAttestationVerifier(
        approved_root=str(root),
        expected_owner_uid=os.geteuid(),
        clock=lambda: now,
    )
    monkeypatch.setenv("ELLA_HERMES_CLOUD_STAGED_ATTESTATION_ENABLED", "true")
    monkeypatch.setenv("ELLA_HERMES_CLOUD_SYNTHETIC_ONLY", "true")
    for name in UID_SELECTORS:
        monkeypatch.setenv(name, uid)
    for name in GLOBAL_FLAGS_REQUIRED_FALSE:
        monkeypatch.setenv(name, "false")

    async def scenario(pool):
        repository, job_id, claimed_model, revision = await _seed_claim(
            pool,
            uid=uid,
            runtime_instance_id=instance,
        )
        assert claimed_model == model
        async with pool.acquire() as conn:
            user_id = await conn.fetchval("SELECT id FROM users WHERE omi_uid = $1", uid)

        receipt = {
            "schema_version": "ella-hermes-cloud-staged-attestation-v1",
            "attestation_id": "attestation-staged-jsonb-01",
            "issued_at": (now - timedelta(minutes=1)).isoformat(),
            "expires_at": (now + timedelta(hours=1)).isoformat(),
            "uid": uid,
            "account_id": str(user_id),
            "profile_id": str(user_id),
            "runtime_instance_id": instance,
            "template_version": "template-v1",
            "voice_policy_version": "voice-policy-v1",
            "expected_model": model,
            "allowed_tools": [],
            "required_capabilities": ["responses_api"],
            "prompt_pack_version": "prompt-v1",
            "model_policy_version": "model-policy-v1",
            "model_context_window_tokens": 16384,
            "policy_commit_sha": "a" * 40,
            "approval_manifest_sha256": "b" * 64,
            "artifact_sha256": {
                "soul": "c" * 64,
                "agents": "d" * 64,
                "model_policy": "e" * 64,
            },
            "stage": "pool_registration_and_claim_finalization",
            "content_free": True,
        }
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        receipt_path.chmod(0o400)
        registration = verifier.preflight(
            {
                "runtime_instance_id": instance,
                "template_version": "template-v1",
                "voice_policy_version": "voice-policy-v1",
                "expected_model": model,
                "allowed_tools": [],
                "required_capabilities": ["responses_api"],
                "prompt_pack_version": "prompt-v1",
                "model_policy_version": "model-policy-v1",
                "prompt_artifact_receipt": _prompt_receipt(model=model),
            },
            receipt_ref=str(receipt_path),
            uid=uid,
            account_id=str(user_id),
            profile_id=str(user_id),
            profile_class="synthetic",
            phase="pool_registration",
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
        assert isinstance(claimed["allowed_tools"], str)
        assert isinstance(claimed["required_capabilities"], str)
        assert json.loads(claimed["allowed_tools"]) == []
        assert json.loads(claimed["required_capabilities"]) == ["responses_api"]

        finalization = verifier.preflight(
            claimed,
            receipt_ref=str(receipt_path),
            uid=uid,
            account_id=str(user_id),
            profile_id=str(user_id),
            profile_class="synthetic",
            phase="claim_finalization",
            prior_marker=registration["staged_attestation"],
        )

        assert finalization["tools"] == []
        assert finalization["capabilities"] == ["responses_api"]
        assert finalization["staged_attestation"]["phase"] == "claim_finalization"

    asyncio.run(_run_with_database(scenario))


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


def test_finalize_ready_cloud_claim_publishes_exact_account_profile_targets(monkeypatch):
    contract = _binding_contract()
    monkeypatch.setenv(
        "ELLA_HERMES_CLOUD_API_URL_SYNTHETIC",
        "https://cloud.example.test",
    )
    monkeypatch.setenv(
        "ELLA_HERMES_CLOUD_API_KEY_SYNTHETIC",
        "synthetic-test-token",
    )
    monkeypatch.setattr(
        runtime_resolver,
        "current_cloud_authority",
        lambda uid, **_kwargs: CurrentCloudAuthority(
            consent_receipt_id=f"receipt-{uid}",
            profile_binding_id=contract["binding"]["profile_binding_id"],
            lineage=LINEAGE,
        ),
    )

    async def scenario(pool):
        uid = contract["uid"]
        instance = contract["binding"]["runtime_instance_id"]
        repository, job_id, model, revision = await _seed_claim(
            pool,
            uid=uid,
            runtime_instance_id=instance,
            allowed_tools=contract["binding"]["allowed_tools"],
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
            admitted_entitlement_revision=revision,
            authority_lineage=LINEAGE,
            status="internal_canary",
            health_receipt={
                "status": "ok",
                "content_free": True,
                **LINEAGE.as_dict(),
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
            "hermes-cloud-photon",
            "hermes-cloud-transcript",
            "hermes-cloud-voice",
        ]
        assert {row["candidate_runtime_instance_id"] for row in targets} == {instance}
        assert {row["endpoint_ref"] for row in targets} == {"env:ELLA_HERMES_CLOUD_API_URL_SYNTHETIC"}
        assert {row["credential_ref"] for row in targets} == {"env:ELLA_HERMES_CLOUD_API_KEY_SYNTHETIC"}
        photon = await repository.resolve_active_runtime(
            uid,
            target_mode="hermes-cloud-photon",
            required_provider="hermes_cloud",
            authority_lineage=LINEAGE,
            model=model,
        )
        assert photon["runtime_target_mode"] == "hermes-cloud-photon"
        assert photon["runtime_target_id"]
        assert photon["runtime_instance_id"] == instance
        assert photon["target_endpoint_ref"] == photon["api_base_url_ref"]
        assert photon["target_credential_ref"] == photon["api_key_ref"]
        assert isinstance(photon["prompt_artifact_receipt"], str)
        assert isinstance(photon["allowed_tools"], str)
        assert isinstance(photon["required_capabilities"], str)
        assert isinstance(photon["id"], uuid.UUID)
        assert isinstance(photon["user_id"], uuid.UUID)
        assert isinstance(photon["account_user_id"], uuid.UUID)
        assert isinstance(photon["profile_user_id"], uuid.UUID)
        runtime = runtime_resolver.runtime_from_binding(photon, uid)
        assert runtime.provider == contract["binding"]["provider"]
        assert runtime.status == contract["binding"]["status"]
        assert runtime.profile_class == contract["binding"]["profile_class"]
        assert runtime.runtime_instance_id == instance
        assert runtime.expected_model == contract["binding"]["expected_model"]
        assert runtime.account_user_id == str(photon["account_user_id"])
        assert runtime.profile_user_id == str(photon["profile_user_id"])
        assert runtime.consent_authority_epoch == str(uuid.UUID(runtime.consent_authority_epoch))
        assert runtime.model_context_window_tokens == 16384
        assert runtime.allowed_tools == tuple(contract["binding"]["allowed_tools"])
        assert runtime.required_capabilities == ("responses_api",)

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


def test_finalize_rechecks_revocation_before_publishing_any_target():
    async def scenario(pool):
        uid = "synthetic-revoked-before-finalize"
        repository, job_id, model, revision = await _seed_claim(
            pool,
            uid=uid,
            runtime_instance_id="synthetic-finalize-race",
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
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE voice_entitlements SET status = 'revoked' WHERE uid = $1",
                uid,
            )

        with pytest.raises(RuntimePoolClaimError) as error:
            await repository.finalize_cloud_pool_claim(
                uid=uid,
                job_id=job_id,
                claim_token=str(claimed["claim_token"]),
                admitted_entitlement_revision=revision,
                authority_lineage=LINEAGE,
                status="internal_canary",
                health_receipt={
                    "status": "ok",
                    "content_free": True,
                    **LINEAGE.as_dict(),
                    "admission_revision": revision,
                },
            )
        assert error.value.code == "runtime_admission_revoked"

        async with pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT COUNT(*) FROM ella_runtime_targets WHERE runtime_binding_id = $1",
                    claimed["id"],
                )
                == 0
            )
            assert (
                await conn.fetchval(
                    "SELECT status FROM ella_runtime_bindings WHERE id = $1",
                    claimed["id"],
                )
                == "claiming"
            )

    asyncio.run(_run_with_database(scenario))


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("expiry", "runtime_admission_expired"),
        ("kill_switch", "runtime_admission_provider_disabled"),
        ("mode_lineage", "runtime_admission_mode_not_allowed"),
        ("consent_lineage", "runtime_cloud_entitlement_lineage_stale"),
    ],
)
def test_finalize_rechecks_every_current_authority_boundary(mutation, expected_code):
    async def scenario(pool):
        uid = f"synthetic-finalize-{mutation}"
        repository, job_id, model, revision = await _seed_claim(
            pool,
            uid=uid,
            runtime_instance_id=f"synthetic-finalize-{mutation}",
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
        async with pool.acquire() as conn:
            if mutation == "expiry":
                await conn.execute(
                    """
                    UPDATE voice_entitlements
                    SET trial_expires_at = CURRENT_TIMESTAMP - INTERVAL '1 second'
                    WHERE uid = $1
                    """,
                    uid,
                )
            elif mutation == "kill_switch":
                await voice_canary.set_kill_switch_on_connection(
                    conn,
                    scope_type="provider",
                    scope_value="hermes_cloud",
                    enabled=True,
                    reason="synthetic finalize race",
                    updated_by="pytest",
                )
            elif mutation == "mode_lineage":
                await conn.execute(
                    """
                    UPDATE voice_entitlements
                    SET mode_allowlist = ARRAY['hermes-cloud-chat']
                    WHERE uid = $1
                    """,
                    uid,
                )
            else:
                await conn.execute(
                    """
                    UPDATE voice_entitlements
                    SET consent_scope_hash = $2
                    WHERE uid = $1
                    """,
                    uid,
                    "sha256:" + ("9" * 64),
                )

        with pytest.raises(RuntimePoolClaimError) as error:
            await repository.finalize_cloud_pool_claim(
                uid=uid,
                job_id=job_id,
                claim_token=str(claimed["claim_token"]),
                admitted_entitlement_revision=revision,
                authority_lineage=LINEAGE,
                status="internal_canary",
                health_receipt={
                    "status": "ok",
                    "content_free": True,
                    **LINEAGE.as_dict(),
                    "admission_revision": revision,
                },
            )
        assert error.value.code == expected_code
        async with pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT COUNT(*) FROM ella_runtime_targets WHERE runtime_binding_id = $1",
                    claimed["id"],
                )
                == 0
            )

    asyncio.run(_run_with_database(scenario))


def test_post_publication_rollback_revokes_cloud_targets_and_preserves_retained_plato():
    async def scenario(pool):
        uid = "synthetic-post-publication-rollback"
        repository, job_id, model, revision = await _seed_claim(
            pool,
            uid=uid,
            runtime_instance_id="synthetic-rollback-instance",
        )
        async with pool.acquire() as conn:
            user_id = await conn.fetchval("SELECT id FROM users WHERE omi_uid = $1", uid)
            await conn.execute(
                """
                INSERT INTO ella_runtime_bindings (
                    user_id, role, provider, status, profile_name, agent_id,
                    workspace_root, internal_gateway_url, gateway_port,
                    service_label, credential_ref, honcho_workspace,
                    observed_peer, observer_peer, template_version,
                    model_policy_version, voice_policy_version, health_state,
                    health_receipt, revision, active
                ) VALUES (
                    $1, 'user', 'hermes', 'active', 'plato-retained-rollback',
                    'plato', '/Users/ellaai/.hermes/profiles/plato-retained-rollback/workspace',
                    'http://127.0.0.1:8765', 8765, 'plato-retained-rollback',
                    'env:HERMES_API_SERVER_KEY', 'plato-retained-workspace',
                    'plato', 'ella', 'hermes-user-v1', 'frontier-v1',
                    'ella-voice-v1', 'healthy', '{"status":"ok"}'::jsonb, 1, true
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
        finalized = await repository.finalize_cloud_pool_claim(
            uid=uid,
            job_id=job_id,
            claim_token=str(claimed["claim_token"]),
            admitted_entitlement_revision=revision,
            authority_lineage=LINEAGE,
            status="internal_canary",
            health_receipt={
                "status": "ok",
                "content_free": True,
                **LINEAGE.as_dict(),
                "admission_revision": revision,
            },
        )
        assert finalized["status"] == "internal_canary"

        rolled_back = await repository.quarantine_cloud_pool_claim(
            uid=uid,
            job_id=job_id,
            claim_token=str(claimed["claim_token"]),
            reason="post_publication_receipt_failure",
            health_receipt={"content_free": True},
        )
        assert rolled_back["status"] == "quarantined"

        async with pool.acquire() as conn:
            target_states = await conn.fetch(
                """
                SELECT status, COUNT(*) AS count
                FROM ella_runtime_targets
                WHERE runtime_binding_id = $1
                GROUP BY status
                """,
                claimed["id"],
            )
            retained = dict(
                await conn.fetchrow(
                    """
                    SELECT provider, status, active, profile_name
                    FROM ella_runtime_bindings
                    WHERE profile_name = 'plato-retained-rollback'
                    """
                )
            )
        assert [dict(row) for row in target_states] == [{"status": "revoked", "count": 5}]
        assert retained == {
            "provider": "hermes",
            "status": "active",
            "active": True,
            "profile_name": "plato-retained-rollback",
        }

    asyncio.run(_run_with_database(scenario))


def test_resolution_rechecks_kill_switch_and_never_returns_retained_binding():
    async def scenario(pool):
        uid = "synthetic-resolution-kill-switch"
        repository, job_id, model, revision = await _seed_claim(
            pool,
            uid=uid,
            runtime_instance_id="synthetic-resolution-instance",
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
        await repository.finalize_cloud_pool_claim(
            uid=uid,
            job_id=job_id,
            claim_token=str(claimed["claim_token"]),
            admitted_entitlement_revision=revision,
            authority_lineage=LINEAGE,
            status="internal_canary",
            health_receipt={
                "status": "ok",
                "content_free": True,
                **LINEAGE.as_dict(),
                "admission_revision": revision,
            },
        )
        resolved = await repository.resolve_active_runtime(
            uid,
            target_mode="hermes-cloud-guardian",
            required_provider="hermes_cloud",
            authority_lineage=LINEAGE,
            model=model,
        )
        assert resolved["provider"] == "hermes_cloud"
        assert resolved["runtime_target_mode"] == "hermes-cloud-guardian"
        assert resolved["runtime_target_id"]
        assert resolved["runtime_target_updated_at"]
        assert resolved["target_endpoint_ref"] == resolved["api_base_url_ref"]
        assert resolved["target_credential_ref"] == resolved["api_key_ref"]
        assert resolved["target_entitlement_revision"] == revision
        async with pool.acquire() as conn:
            consent_authority_epoch = await conn.fetchval(
                """
                SELECT consent_authority_epoch
                FROM voice_entitlements
                WHERE uid = $1
                """,
                uid,
            )
        assert resolved["consent_authority_epoch"] == str(consent_authority_epoch)

        async with pool.acquire() as conn:
            await voice_canary.set_kill_switch_on_connection(
                conn,
                scope_type="provider",
                scope_value="hermes_cloud",
                enabled=True,
                reason="synthetic resolution race",
                updated_by="pytest",
            )
        with pytest.raises(RuntimePoolClaimError) as error:
            await repository.resolve_active_runtime(
                uid,
                target_mode="hermes-cloud-guardian",
                required_provider="hermes_cloud",
                authority_lineage=LINEAGE,
                model=model,
            )
        assert error.value.code == "runtime_admission_provider_disabled"

    asyncio.run(_run_with_database(scenario))
