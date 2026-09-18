import asyncio
import hashlib
import hmac
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg
import pytest

from database import authority_advisory_lock
from database.ella_provisioning import invalidate_self_hosted_authority_on_connection
from database.imessage_enrollment import (
    ImessageAuthorityError,
    ImessageConsentContract,
    ImessageConsentInput,
    ImessageEnrollmentRepository,
    ImessageRuntimeSnapshot,
)
from database.imessage_runtime import (
    ImessageRuntimeAuthority,
    ImessageRuntimeRepository,
    ImessageRuntimeRepositoryError,
)
from database.imessage_retained_runtime import (
    RetainedImessageRuntimeError,
    RetainedImessageRuntimeRepository,
    RetainedImessageRuntimeSpec,
)

TEST_DSN = os.getenv("ELLA_TEST_POSTGRES_DSN", "").strip()
MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"

pytestmark = pytest.mark.skipif(
    not TEST_DSN,
    reason="ELLA_TEST_POSTGRES_DSN is required for iMessage enrollment PostgreSQL tests",
)

BASE_PROVISIONING_SCHEMA = """
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    omi_uid TEXT UNIQUE,
    email TEXT UNIQUE,
    name TEXT NOT NULL DEFAULT 'Enrollment User',
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
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, target_schema_version)
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

MIGRATION_CHAIN = (
    "008_create_voice_canary_controls.sql",
    "009_create_hermes_cloud_runtime_pool.sql",
    "010_add_cloud_profile_class.sql",
    "011_create_invitation_redemption.sql",
    "012_create_account_profile_runtime_targets.sql",
    "013_create_managed_cloud_consent_authority.sql",
    "014_add_synthetic_invitation_operator_audit.sql",
    "015_add_invitation_allowed_email_hash.sql",
    "020_create_imessage_enrollment_authority.sql",
    "021_create_imessage_runtime_outbox.sql",
    "022_add_imessage_retained_runtime_authority.sql",
    "023_add_imessage_runtime_binding_role.sql",
)

POLICY = "ella-imessage-data-v2"
PROCESSOR_HASH = "sha256:" + ("1" * 64)
SCOPE = "ella.imessage_text_dm.v1"
SCOPE_HASH = "sha256:" + ("2" * 64)
CONSENT_CONTRACT = ImessageConsentContract(
    policy_version=POLICY,
    processor_set_hash=PROCESSOR_HASH,
    scope_version=SCOPE,
    scope_hash=SCOPE_HASH,
)
PROOF_KEY = b"p" * 32


def _proof_hash(*, salt: str, code: str) -> str:
    return hmac.new(
        PROOF_KEY,
        f"ella-imessage-proof-hash-v1:{salt}:{code}".encode(),
        hashlib.sha256,
    ).hexdigest()


async def _run_with_database(scenario, *, migration_chain=MIGRATION_CHAIN):
    schema = f"imessage_enrollment_{uuid.uuid4().hex}"
    admin = await asyncpg.connect(TEST_DSN)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(
        TEST_DSN,
        min_size=1,
        max_size=4,
        server_settings={"search_path": schema},
    )
    try:
        async with pool.acquire() as connection:
            await connection.execute(BASE_PROVISIONING_SCHEMA)
            await connection.execute((MIGRATIONS / "008_create_voice_canary_controls.sql").read_text(encoding="utf-8"))
            await connection.execute(
                (MIGRATIONS / "009_create_hermes_cloud_runtime_pool.sql").read_text(encoding="utf-8")
            )
            photon_columns_before = await connection.fetchval(
                """
                SELECT jsonb_agg(column_name ORDER BY ordinal_position)
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'ella_photon_channel_bindings'
                """
            )
            for name in migration_chain[2:]:
                await connection.execute((MIGRATIONS / name).read_text(encoding="utf-8"))
            photon_columns_after = await connection.fetchval(
                """
                SELECT jsonb_agg(column_name ORDER BY ordinal_position)
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'ella_photon_channel_bindings'
                """
            )
            assert photon_columns_after == photon_columns_before
        await scenario(pool)
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def _seed_owner(pool, *, uid: str, ordinal: int):
    async with pool.acquire() as connection:
        user_id = await connection.fetchval(
            "INSERT INTO users (omi_uid, profile_class) VALUES ($1, 'real') RETURNING id",
            uid,
        )
        reservation_id = await connection.fetchval(
            """
            INSERT INTO ella_invitation_capacity_reservations (pool_key, state)
            VALUES ($1, 'consumed') RETURNING id
            """,
            f"imessage-{ordinal}",
        )
        invitation_id = await connection.fetchval(
            """
            INSERT INTO ella_invitations (
                capacity_reservation_id, kind, code_hmac, state,
                delivery_state, usage_mode, max_redemptions,
                reserved_setup_slots, entitlement_policy_revision,
                entitlement_policy, required_consent_policy_version,
                required_consent_processor_set_hash,
                required_consent_scope_version, required_consent_scope_hash,
                cohort, exclude_from_product_analytics
            ) VALUES (
                $1, 'ordinary', $2, 'issued', 'pending', 'single_use', 1, 1,
                'imessage-v1', '{}'::jsonb, $3, $4, $5, $6,
                'founding_family', FALSE
            ) RETURNING id
            """,
            reservation_id,
            f"{ordinal:064x}",
            POLICY,
            PROCESSOR_HASH,
            SCOPE,
            SCOPE_HASH,
        )
        invitation_target_id = await connection.fetchval(
            """
            INSERT INTO ella_invitation_targets (
                invitation_id, account_ref_hmac, profile_ref_hmac,
                required_profile_class
            ) VALUES ($1, $2, $3, 'real')
            RETURNING id
            """,
            invitation_id,
            hashlib.sha256(f"account-{ordinal}".encode()).hexdigest(),
            hashlib.sha256(f"profile-{ordinal}".encode()).hexdigest(),
        )
        binding_id = await connection.fetchval(
            """
            INSERT INTO ella_runtime_bindings (
                user_id, account_user_id, profile_user_id, role, provider,
                profile_name, agent_id, template_version,
                model_policy_version, voice_policy_version, health_state,
                revision, active, status
            ) VALUES (
                $1, $1, $1, 'user', 'hermes', $2, $3,
                'template-v1', 'model-v1', 'voice-v1', 'healthy', 3, TRUE, 'active'
            ) RETURNING id
            """,
            user_id,
            f"imessage-profile-{ordinal}",
            f"imessage-agent-{ordinal}",
        )
        target_id = await connection.fetchval(
            """
            INSERT INTO ella_runtime_targets (
                account_user_id, profile_user_id, role, mode, provider,
                runtime_binding_id, status, policy_version,
                processor_set_hash, scope_version, scope_hash,
                entitlement_revision, invitation_target_id
            ) VALUES (
                $1, $1, 'user', 'hermes-chat', 'hermes', $2, 'ready',
                $3, $4, $5, $6, 4, $7
            ) RETURNING id
            """,
            user_id,
            binding_id,
            POLICY,
            PROCESSOR_HASH,
            SCOPE,
            SCOPE_HASH,
            invitation_target_id,
        )
    return user_id, ImessageRuntimeSnapshot(
        uid=uid,
        binding_id=binding_id,
        target_id=target_id,
        authority_kind="target",
        authority_digest=hashlib.sha256(f"runtime-{ordinal}".encode()).hexdigest(),
        binding_revision=3,
        entitlement_revision=4,
        account_user_id=user_id,
        profile_user_id=user_id,
        binding_role="user",
    )


async def _seed_retained_owner(pool, *, uid: str, ordinal: int):
    user_id, targeted = await _seed_owner(pool, uid=uid, ordinal=ordinal)
    gateway_port = 9000 + ordinal
    async with pool.acquire() as connection:
        binding_id = await connection.fetchval(
            """
            INSERT INTO ella_runtime_bindings (
                user_id, account_user_id, profile_user_id, role, provider,
                profile_name, agent_id, workspace_root, internal_gateway_url,
                gateway_port, service_label, credential_ref, honcho_workspace,
                observed_peer, observer_peer, template_version,
                model_policy_version, voice_policy_version, health_state,
                revision, active, status
            ) VALUES (
                $1, $1, $1, 'imessage', 'hermes', $2, $3, $4, $5,
                $6, $7, $8, $9, $10, $11, 'template-v1',
                'model-v1', 'voice-v1', 'healthy', 3, TRUE, 'active'
            ) RETURNING id
            """,
            user_id,
            f"retained-imessage-profile-{ordinal}",
            f"retained-imessage-agent-{ordinal}",
            f"/Users/ellaai/.hermes/profiles/retained-imessage-profile-{ordinal}/workspace",
            f"http://127.0.0.1:{gateway_port}",
            gateway_port,
            f"ai.hermes.gateway-imessage-{ordinal}",
            f"IMESSAGE_GATEWAY_TOKEN_{ordinal}",
            f"imessage-honcho-{ordinal}",
            f"imessage-observed-{ordinal}",
            f"imessage-observer-{ordinal}",
        )
    return user_id, ImessageRuntimeSnapshot(
        uid=uid,
        binding_id=binding_id,
        target_id=None,
        authority_kind="retained_owner",
        authority_digest=hashlib.sha256(f"retained-runtime-{ordinal}".encode()).hexdigest(),
        binding_revision=targeted.binding_revision,
        entitlement_revision=0,
        account_user_id=user_id,
        profile_user_id=user_id,
        binding_role="imessage",
    )


async def _grant(repository, *, uid: str, ordinal: int):
    return await repository.submit_consent(
        uid=uid,
        submission=ImessageConsentInput(
            request_id=uuid.uuid5(uuid.NAMESPACE_URL, f"consent-{ordinal}"),
            decision="granted",
            policy_version=POLICY,
            processor_set_hash=PROCESSOR_HASH,
            scope_version=SCOPE,
            scope_hash=SCOPE_HASH,
            app_version="1.0",
            build_number="1",
        ),
    )


async def _runtime_authority(pool, *, uid: str, ordinal: int, runtime: ImessageRuntimeSnapshot):
    async with pool.acquire() as connection:
        target_updated_at = await connection.fetchval(
            "SELECT updated_at FROM ella_runtime_targets WHERE id = $1",
            runtime.target_id,
        )
    return ImessageRuntimeAuthority(
        uid=uid,
        user_id=runtime.account_user_id,
        profile_user_id=runtime.profile_user_id,
        runtime_binding_id=runtime.binding_id,
        runtime_target_id=runtime.target_id,
        runtime_authority_kind=runtime.authority_kind,
        runtime_binding_revision=runtime.binding_revision,
        runtime_target_entitlement_revision=runtime.entitlement_revision,
        runtime_target_updated_at=target_updated_at,
        runtime_authority_digest=runtime.authority_digest,
        runtime_agent_id=(
            f"retained-imessage-agent-{ordinal}"
            if runtime.authority_kind == "retained_owner"
            else f"imessage-agent-{ordinal}"
        ),
        runtime_instance_id=None,
        runtime_profile_name=(
            f"retained-imessage-profile-{ordinal}"
            if runtime.authority_kind == "retained_owner"
            else f"imessage-profile-{ordinal}"
        ),
        runtime_binding_role=runtime.binding_role,
    )


async def _pending_binding(repository, *, uid: str, ordinal: int, runtime, receipt):
    attempt, created = await repository.prepare_registration(
        uid=uid,
        idempotency_key=uuid.uuid5(uuid.NAMESPACE_URL, f"enrollment-{ordinal}"),
        handset_ref_hmac=hashlib.sha256(f"handset-{ordinal}".encode()).hexdigest(),
        consent_receipt_id=receipt["id"],
        consent_contract=CONSENT_CONTRACT,
        runtime=runtime,
    )
    assert created is True
    registration_hmac = hashlib.sha256(f"registration-{ordinal}".encode()).hexdigest()
    destination_hmac = hashlib.sha256(f"destination-{ordinal}".encode()).hexdigest()
    attempt = await repository.mark_provider_accepted(
        uid=uid,
        attempt_id=attempt["id"],
        provider_registration_ref_hmac=registration_hmac,
        assigned_destination_e164=f"+15555550{ordinal:03d}",
        assigned_destination_ref_hmac=destination_hmac,
    )
    code = f"{ordinal:06d}"
    salt = f"{ordinal:032x}"
    binding, created = await repository.finalize_registration(
        uid=uid,
        attempt_id=attempt["id"],
        runtime=runtime,
        provider_registration_ref_hmac=registration_hmac,
        assigned_destination_e164=attempt["assigned_destination_e164"],
        assigned_destination_ref_hmac=destination_hmac,
        challenge_salt=salt,
        challenge_hash=_proof_hash(salt=salt, code=code),
        challenge_expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
        consent_contract=CONSENT_CONTRACT,
    )
    assert created is True
    return binding, code, destination_hmac


def test_migration_and_repository_enforce_consent_proof_replay_and_revoke():
    async def scenario(pool):
        repository = ImessageEnrollmentRepository(pool)
        await repository.assert_schema_ready()
        uid = "imessage-owner-a"
        _, runtime = await _seed_owner(pool, uid=uid, ordinal=1)
        previous_receipt = await repository.submit_consent(
            uid=uid,
            submission=ImessageConsentInput(
                request_id=uuid.uuid5(uuid.NAMESPACE_URL, "consent-v1-1"),
                decision="granted",
                policy_version="ella-imessage-data-v1",
                processor_set_hash=PROCESSOR_HASH,
                scope_version=SCOPE,
                scope_hash=SCOPE_HASH,
                app_version="1.0",
                build_number="1",
            ),
        )
        assert POLICY == "ella-imessage-data-v2"
        with pytest.raises(ImessageAuthorityError, match="imessage_consent_policy_stale"):
            await repository.prepare_registration(
                uid=uid,
                idempotency_key=uuid.uuid5(uuid.NAMESPACE_URL, "stale-consent-attempt"),
                handset_ref_hmac=hashlib.sha256(b"stale-handset").hexdigest(),
                consent_receipt_id=previous_receipt["id"],
                consent_contract=CONSENT_CONTRACT,
                runtime=runtime,
            )
        receipt = await _grant(repository, uid=uid, ordinal=1)
        same_receipt = await _grant(repository, uid=uid, ordinal=1)
        assert same_receipt["id"] == receipt["id"]

        binding, code, destination_hmac = await _pending_binding(
            repository,
            uid=uid,
            ordinal=1,
            runtime=runtime,
            receipt=receipt,
        )
        rejected_message = hashlib.sha256(b"message-rejected").hexdigest()
        with pytest.raises(ImessageAuthorityError, match="imessage_proof_invalid"):
            await repository.verify_inbound_proof(
                assigned_destination_ref_hmac=destination_hmac,
                handset_ref_hmac=hashlib.sha256(b"handset-1").hexdigest(),
                line_identity_hmac=hashlib.sha256(b"line-a").hexdigest(),
                contact_identity_hmac=hashlib.sha256(b"contact-a").hexdigest(),
                provider_message_ref_hmac=rejected_message,
                candidate_challenge_hash=_proof_hash(salt=f"{1:032x}", code="999999"),
                consent_contract=CONSENT_CONTRACT,
                runtime=runtime,
                now=datetime.now(timezone.utc),
            )
        async with pool.acquire() as connection:
            rejected_state = await connection.fetchrow(
                """
                SELECT
                    b.challenge_attempts,
                    b.status,
                    EXISTS (
                        SELECT 1
                        FROM ella_imessage_proof_receipts r
                        WHERE r.provider_message_ref_hmac = $2
                          AND r.outcome = 'rejected'
                    ) AS receipt_exists
                FROM ella_imessage_channel_bindings b
                WHERE b.id = $1
                """,
                binding["id"],
                rejected_message,
            )
        assert int(rejected_state["challenge_attempts"]) == 1
        assert rejected_state["status"] == "verification_pending"
        assert rejected_state["receipt_exists"] is True

        activated = await repository.verify_inbound_proof(
            assigned_destination_ref_hmac=destination_hmac,
            handset_ref_hmac=hashlib.sha256(b"handset-1").hexdigest(),
            line_identity_hmac=hashlib.sha256(b"line-a").hexdigest(),
            contact_identity_hmac=hashlib.sha256(b"contact-a").hexdigest(),
            provider_message_ref_hmac=hashlib.sha256(b"message-a").hexdigest(),
            candidate_challenge_hash=_proof_hash(salt=f"{1:032x}", code=code),
            consent_contract=CONSENT_CONTRACT,
            runtime=runtime,
            now=datetime.now(timezone.utc),
        )
        assert activated["status"] == "active"

        with pytest.raises(ImessageAuthorityError, match="imessage_proof_replayed"):
            await repository.verify_inbound_proof(
                assigned_destination_ref_hmac=destination_hmac,
                handset_ref_hmac=hashlib.sha256(b"handset-1").hexdigest(),
                line_identity_hmac=hashlib.sha256(b"line-a").hexdigest(),
                contact_identity_hmac=hashlib.sha256(b"contact-a").hexdigest(),
                provider_message_ref_hmac=hashlib.sha256(b"message-a").hexdigest(),
                candidate_challenge_hash=_proof_hash(salt=f"{1:032x}", code=code),
                consent_contract=CONSENT_CONTRACT,
                runtime=runtime,
                now=datetime.now(timezone.utc),
            )

        revoked = await repository.revoke_binding(
            uid=uid,
            expected_generation=int(binding["generation"]),
            idempotency_key=uuid.uuid5(uuid.NAMESPACE_URL, "revoke-1"),
        )
        assert revoked["status"] == "revoked"
        assert int(revoked["generation"]) == int(binding["generation"]) + 1

        async with pool.acquire() as connection:
            with pytest.raises(asyncpg.PostgresError):
                await connection.execute(
                    "UPDATE ella_imessage_consent_receipts SET app_version = 'changed' WHERE id = $1",
                    receipt["id"],
                )

    asyncio.run(_run_with_database(scenario))


def test_retained_authority_migration_upgrades_existing_target_rows_without_rewriting_identity():
    async def scenario(pool):
        repository = ImessageEnrollmentRepository(pool)
        uid = "imessage-pre-retained-migration"
        user_id, runtime = await _seed_owner(pool, uid=uid, ordinal=17)
        consent = await _grant(repository, uid=uid, ordinal=17)
        attempt_id = uuid.uuid4()
        channel_id = uuid.uuid4()
        receipt_id = uuid.uuid4()
        authority_digest = hashlib.sha256(b"pre-retained-authority").hexdigest()
        async with pool.acquire() as connection:
            authority_epoch = await connection.fetchval(
                "SELECT authority_epoch FROM ella_imessage_consent_authority WHERE user_id = $1",
                user_id,
            )
            await connection.execute(
                """
                INSERT INTO ella_imessage_registration_attempts (
                    id, user_id, idempotency_key, handset_ref_hmac,
                    consent_receipt_id, consent_authority_epoch,
                    runtime_binding_id, runtime_target_id, runtime_authority_digest,
                    state, provider_request_id, provider_registration_ref_hmac,
                    assigned_destination_e164, assigned_destination_ref_hmac
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9,
                    'provider_accepted', $10, $11, $12, $13
                )
                """,
                attempt_id,
                user_id,
                uuid.uuid4(),
                hashlib.sha256(b"pre-retained-handset").hexdigest(),
                consent["id"],
                authority_epoch,
                runtime.binding_id,
                runtime.target_id,
                authority_digest,
                uuid.uuid4(),
                hashlib.sha256(b"pre-retained-registration").hexdigest(),
                "+15555550117",
                hashlib.sha256(b"pre-retained-destination").hexdigest(),
            )
            await connection.execute(
                """
                INSERT INTO ella_imessage_channel_bindings (
                    id, user_id, registration_attempt_id, status, generation,
                    handset_ref_hmac, assigned_destination_e164,
                    assigned_destination_ref_hmac, line_identity_hmac,
                    contact_identity_hmac, provider_registration_ref_hmac,
                    runtime_binding_id, runtime_target_id, runtime_authority_digest,
                    consent_receipt_id, consent_authority_epoch,
                    challenge_salt, challenge_hash, challenge_expires_at, verified_at
                ) VALUES (
                    $1, $2, $3, 'active', 1, $4, $5, $6, $7, $8, $9,
                    $10, $11, $12, $13, $14, $15, $16,
                    CURRENT_TIMESTAMP + INTERVAL '15 minutes', CURRENT_TIMESTAMP
                )
                """,
                channel_id,
                user_id,
                attempt_id,
                hashlib.sha256(b"pre-retained-handset").hexdigest(),
                "+15555550117",
                hashlib.sha256(b"pre-retained-destination").hexdigest(),
                hashlib.sha256(b"pre-retained-line").hexdigest(),
                hashlib.sha256(b"pre-retained-contact").hexdigest(),
                hashlib.sha256(b"pre-retained-registration").hexdigest(),
                runtime.binding_id,
                runtime.target_id,
                authority_digest,
                consent["id"],
                authority_epoch,
                "1" * 32,
                "2" * 64,
            )
            await connection.execute(
                """
                INSERT INTO ella_imessage_message_receipts (
                    id, binding_id, user_id, inbound_provider_ref_hmac,
                    inbound_payload_sha256, message_text, occurred_at,
                    binding_generation, consent_receipt_id, consent_authority_epoch,
                    runtime_binding_id, runtime_target_id, runtime_authority_digest,
                    status, lease_token, lease_expires_at
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, CURRENT_TIMESTAMP,
                    1, $7, $8, $9, $10, $11,
                    'claimed', $12, CURRENT_TIMESTAMP + INTERVAL '1 minute'
                )
                """,
                receipt_id,
                channel_id,
                user_id,
                hashlib.sha256(b"pre-retained-message").hexdigest(),
                hashlib.sha256(b"pre-retained-payload").hexdigest(),
                "content-free migration fixture",
                consent["id"],
                authority_epoch,
                runtime.binding_id,
                runtime.target_id,
                authority_digest,
                uuid.uuid4(),
            )
            for name in MIGRATION_CHAIN[-2:]:
                await connection.execute((MIGRATIONS / name).read_text(encoding="utf-8"))
            rows = await connection.fetchrow(
                """
                SELECT
                    a.runtime_authority_kind AS attempt_kind,
                    a.runtime_binding_role AS attempt_role,
                    a.runtime_target_id AS attempt_target,
                    b.runtime_authority_kind AS binding_kind,
                    b.runtime_binding_role AS binding_role,
                    b.runtime_target_id AS binding_target,
                    r.runtime_authority_kind AS receipt_kind,
                    r.runtime_binding_role AS receipt_role,
                    r.runtime_target_id AS receipt_target
                FROM ella_imessage_registration_attempts a
                JOIN ella_imessage_channel_bindings b ON b.registration_attempt_id = a.id
                JOIN ella_imessage_message_receipts r ON r.binding_id = b.id
                WHERE a.id = $1
                """,
                attempt_id,
            )
            assert dict(rows) == {
                "attempt_kind": "target",
                "attempt_role": "user",
                "attempt_target": runtime.target_id,
                "binding_kind": "target",
                "binding_role": "user",
                "binding_target": runtime.target_id,
                "receipt_kind": "target",
                "receipt_role": "user",
                "receipt_target": runtime.target_id,
            }
            with pytest.raises(asyncpg.CheckViolationError):
                await connection.execute(
                    "UPDATE ella_imessage_message_receipts SET runtime_target_id = NULL WHERE id = $1",
                    receipt_id,
                )

            # The successor remains safe for the final schema shape that was
            # briefly shipped by the rewritten migration 022.
            await connection.execute(
                (MIGRATIONS / "023_add_imessage_runtime_binding_role.sql").read_text(encoding="utf-8")
            )

    asyncio.run(_run_with_database(scenario, migration_chain=MIGRATION_CHAIN[:-2]))


def test_runtime_binding_role_migration_upgrades_published_022_retained_rows():
    async def scenario(pool):
        repository = ImessageEnrollmentRepository(pool)
        uid = "imessage-published-022-retained"
        user_id, runtime = await _seed_retained_owner(pool, uid=uid, ordinal=18)
        consent = await _grant(repository, uid=uid, ordinal=18)
        attempt_id = uuid.uuid4()
        channel_id = uuid.uuid4()
        receipt_id = uuid.uuid4()

        async with pool.acquire() as connection:
            role_column_count = await connection.fetchval(
                """
                SELECT count(*)
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name IN (
                      'ella_imessage_registration_attempts',
                      'ella_imessage_channel_bindings',
                      'ella_imessage_message_receipts'
                  )
                  AND column_name = 'runtime_binding_role'
                """
            )
            assert role_column_count == 0

            authority_epoch = await connection.fetchval(
                "SELECT authority_epoch FROM ella_imessage_consent_authority WHERE user_id = $1",
                user_id,
            )
            registration_hmac = hashlib.sha256(b"published-022-registration").hexdigest()
            handset_hmac = hashlib.sha256(b"published-022-handset").hexdigest()
            destination_hmac = hashlib.sha256(b"published-022-destination").hexdigest()
            await connection.execute(
                """
                INSERT INTO ella_imessage_registration_attempts (
                    id, user_id, idempotency_key, handset_ref_hmac,
                    consent_receipt_id, consent_authority_epoch,
                    runtime_binding_id, runtime_target_id, runtime_authority_kind,
                    runtime_authority_digest, state, provider_request_id,
                    provider_registration_ref_hmac, assigned_destination_e164,
                    assigned_destination_ref_hmac
                ) VALUES (
                    $1, $2, $3, $4, $5, $6,
                    $7, NULL, 'retained_owner', $8, 'provider_accepted', $9,
                    $10, $11, $12
                )
                """,
                attempt_id,
                user_id,
                uuid.uuid4(),
                handset_hmac,
                consent["id"],
                authority_epoch,
                runtime.binding_id,
                runtime.authority_digest,
                uuid.uuid4(),
                registration_hmac,
                "+15555550118",
                destination_hmac,
            )
            await connection.execute(
                """
                INSERT INTO ella_imessage_channel_bindings (
                    id, user_id, registration_attempt_id, status, generation,
                    handset_ref_hmac, assigned_destination_e164,
                    assigned_destination_ref_hmac, line_identity_hmac,
                    contact_identity_hmac, provider_registration_ref_hmac,
                    runtime_binding_id, runtime_target_id, runtime_authority_kind,
                    runtime_authority_digest, consent_receipt_id,
                    consent_authority_epoch, challenge_salt, challenge_hash,
                    challenge_expires_at, verified_at
                ) VALUES (
                    $1, $2, $3, 'active', 1, $4, $5, $6, $7, $8, $9,
                    $10, NULL, 'retained_owner', $11, $12, $13, $14, $15,
                    CURRENT_TIMESTAMP + INTERVAL '15 minutes', CURRENT_TIMESTAMP
                )
                """,
                channel_id,
                user_id,
                attempt_id,
                handset_hmac,
                "+15555550118",
                destination_hmac,
                hashlib.sha256(b"published-022-line").hexdigest(),
                hashlib.sha256(b"published-022-contact").hexdigest(),
                registration_hmac,
                runtime.binding_id,
                runtime.authority_digest,
                consent["id"],
                authority_epoch,
                "3" * 32,
                "4" * 64,
            )
            await connection.execute(
                """
                INSERT INTO ella_imessage_message_receipts (
                    id, binding_id, user_id, inbound_provider_ref_hmac,
                    inbound_payload_sha256, message_text, occurred_at,
                    binding_generation, consent_receipt_id, consent_authority_epoch,
                    runtime_binding_id, runtime_target_id, runtime_authority_kind,
                    runtime_authority_digest, status, lease_token, lease_expires_at
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, CURRENT_TIMESTAMP,
                    1, $7, $8, $9, NULL, 'retained_owner', $10,
                    'claimed', $11, CURRENT_TIMESTAMP + INTERVAL '1 minute'
                )
                """,
                receipt_id,
                channel_id,
                user_id,
                hashlib.sha256(b"published-022-message").hexdigest(),
                hashlib.sha256(b"published-022-payload").hexdigest(),
                "content-free published migration fixture",
                consent["id"],
                authority_epoch,
                runtime.binding_id,
                runtime.authority_digest,
                uuid.uuid4(),
            )

            await connection.execute(
                (MIGRATIONS / "023_add_imessage_runtime_binding_role.sql").read_text(encoding="utf-8")
            )
            migrated = await connection.fetchrow(
                """
                SELECT
                    a.id AS attempt_id,
                    a.runtime_binding_role AS attempt_role,
                    b.id AS binding_id,
                    b.runtime_binding_role AS binding_role,
                    r.id AS receipt_id,
                    r.runtime_binding_role AS receipt_role
                FROM ella_imessage_registration_attempts a
                JOIN ella_imessage_channel_bindings b ON b.registration_attempt_id = a.id
                JOIN ella_imessage_message_receipts r ON r.binding_id = b.id
                WHERE a.id = $1
                """,
                attempt_id,
            )
            assert dict(migrated) == {
                "attempt_id": attempt_id,
                "attempt_role": "imessage",
                "binding_id": channel_id,
                "binding_role": "imessage",
                "receipt_id": receipt_id,
                "receipt_role": "imessage",
            }
            with pytest.raises(asyncpg.CheckViolationError):
                await connection.execute(
                    "UPDATE ella_imessage_channel_bindings SET runtime_binding_role = 'user' WHERE id = $1",
                    channel_id,
                )

            await connection.execute(
                (MIGRATIONS / "023_add_imessage_runtime_binding_role.sql").read_text(encoding="utf-8")
            )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM ella_imessage_message_receipts WHERE id = $1",
                    receipt_id,
                )
                == 1
            )

    asyncio.run(_run_with_database(scenario, migration_chain=MIGRATION_CHAIN[:-1]))


def test_retained_imessage_binding_admin_is_two_phase_exact_and_rollback_safe():
    async def scenario(pool):
        uid = "imessage-retained-admin-owner"
        user_id, ordinary_runtime = await _seed_owner(pool, uid=uid, ordinal=19)
        repository = RetainedImessageRuntimeRepository(pool, owner_uid=uid)
        spec = RetainedImessageRuntimeSpec(
            profile_name="plato-eval",
            agent_id="plato-eval",
            workspace_root="/Users/ellaai/.hermes/profiles/plato-eval/workspace",
            internal_gateway_url="http://127.0.0.1:8657",
            gateway_port=8657,
            service_label="ai.hermes.gateway-plato-eval",
            credential_ref="ELLA_IMESSAGE_PLATO_EVAL_GATEWAY_TOKEN",
            honcho_workspace="plato-eval-retained",
            observed_peer="plato-eval-owner",
            observer_peer="plato-eval-observer",
            template_version="retained-v1",
            model_policy_version="retained-model-v1",
            voice_policy_version="retained-voice-v1",
        )
        manifest_sha256 = hashlib.sha256(b"retained-admin-manifest").hexdigest()
        health_sha256 = hashlib.sha256(b"retained-admin-health").hexdigest()

        _, collision_runtime = await _seed_owner(
            pool,
            uid="imessage-retained-admin-collision",
            ordinal=21,
        )
        async with pool.acquire() as connection:
            colliding_service_label = await connection.fetchval(
                "UPDATE ella_runtime_bindings SET service_label = $2 WHERE id = $1 RETURNING service_label",
                collision_runtime.binding_id,
                spec.service_label,
            )
        assert colliding_service_label == spec.service_label
        with pytest.raises(
            RetainedImessageRuntimeError,
            match="imessage_retained_physical_identity_conflict",
        ):
            await repository.stage(uid=uid, spec=spec, manifest_sha256=manifest_sha256)
        async with pool.acquire() as connection:
            await connection.execute(
                "UPDATE ella_runtime_bindings SET service_label = $2 WHERE id = $1",
                collision_runtime.binding_id,
                "ai.hermes.gateway-imessage-21",
            )

        staged, created = await repository.stage(uid=uid, spec=spec, manifest_sha256=manifest_sha256)
        assert created is True
        assert staged["role"] == "imessage"
        assert staged["active"] is False
        assert staged["status"] == "disabled"
        assert staged["health_state"] == "pending"
        same, created = await repository.stage(uid=uid, spec=spec, manifest_sha256=manifest_sha256)
        assert created is False
        assert same["id"] == staged["id"]

        with pytest.raises(RetainedImessageRuntimeError, match="imessage_retained_binding_conflict"):
            await repository.stage(
                uid=uid,
                spec=RetainedImessageRuntimeSpec(**{**spec.__dict__, "agent_id": "wrong-agent"}),
                manifest_sha256=manifest_sha256,
            )
        with pytest.raises(RetainedImessageRuntimeError, match="imessage_retained_owner_forbidden"):
            await repository.stage(uid="other-owner", spec=spec, manifest_sha256=manifest_sha256)

        async with pool.acquire() as connection:
            await connection.execute(
                "UPDATE ella_runtime_bindings SET agent_id = 'drifted-agent' WHERE id = $1",
                staged["id"],
            )
        with pytest.raises(RetainedImessageRuntimeError, match="imessage_retained_binding_conflict"):
            await repository.activate(
                uid=uid,
                spec=spec,
                manifest_sha256=manifest_sha256,
                health_receipt_sha256=health_sha256,
            )
        async with pool.acquire() as connection:
            await connection.execute(
                "UPDATE ella_runtime_bindings SET agent_id = $2 WHERE id = $1",
                staged["id"],
                spec.agent_id,
            )

        activated, changed = await repository.activate(
            uid=uid,
            spec=spec,
            manifest_sha256=manifest_sha256,
            health_receipt_sha256=health_sha256,
        )
        assert changed is True
        assert activated["active"] is True
        assert activated["status"] == "active"
        assert activated["health_state"] == "healthy"
        same, changed = await repository.activate(
            uid=uid,
            spec=spec,
            manifest_sha256=manifest_sha256,
            health_receipt_sha256=health_sha256,
        )
        assert changed is False
        assert same["id"] == activated["id"]

        async with pool.acquire() as connection:
            ordinary = await connection.fetchrow(
                "SELECT id, role, active FROM ella_runtime_bindings WHERE id = $1",
                ordinary_runtime.binding_id,
            )
            target_count = await connection.fetchval(
                "SELECT COUNT(*) FROM ella_runtime_targets WHERE runtime_binding_id = $1",
                ordinary_runtime.binding_id,
            )
        assert ordinary["role"] == "user"
        assert ordinary["active"] is True
        assert int(target_count) == 1

        runtime = ImessageRuntimeSnapshot(
            uid=uid,
            binding_id=activated["id"],
            target_id=None,
            authority_kind="retained_owner",
            authority_digest=hashlib.sha256(b"retained-admin-authority").hexdigest(),
            binding_revision=int(activated["revision"]),
            entitlement_revision=0,
            account_user_id=user_id,
            profile_user_id=user_id,
            binding_role="imessage",
        )
        enrollment = ImessageEnrollmentRepository(pool)
        consent = await _grant(enrollment, uid=uid, ordinal=19)
        await _pending_binding(
            enrollment,
            uid=uid,
            ordinal=19,
            runtime=runtime,
            receipt=consent,
        )
        with pytest.raises(RetainedImessageRuntimeError, match="imessage_retained_graph_not_empty"):
            await repository.stage(uid=uid, spec=spec, manifest_sha256=manifest_sha256)
        with pytest.raises(RetainedImessageRuntimeError, match="imessage_retained_binding_in_use"):
            await repository.rollback(uid=uid, manifest_sha256=manifest_sha256)

        async with pool.acquire() as connection:
            await connection.execute(
                "DELETE FROM ella_imessage_channel_bindings WHERE runtime_binding_id = $1",
                activated["id"],
            )
            await connection.execute(
                "DELETE FROM ella_imessage_registration_attempts WHERE runtime_binding_id = $1",
                activated["id"],
            )
        assert await repository.rollback(uid=uid, manifest_sha256=manifest_sha256) is True
        assert await repository.rollback(uid=uid, manifest_sha256=manifest_sha256) is False

    asyncio.run(_run_with_database(scenario))


def test_invitation_revocation_preserves_retained_role_but_account_deletion_disables_it():
    async def scenario(pool):
        uid = "imessage-retained-lifecycle-owner"
        user_id, runtime = await _seed_retained_owner(pool, uid=uid, ordinal=20)
        owner = authority_advisory_lock.AuthorityOwner.from_values(user_id, user_id)
        async with pool.acquire() as connection:
            async with connection.transaction():
                proof = await authority_advisory_lock.acquire_authority_lock(connection, owner=owner)
                await authority_advisory_lock.verify_self_owner_after_lock(
                    connection,
                    uid=uid,
                    owner=owner,
                    proof=proof,
                )
                await invalidate_self_hosted_authority_on_connection(
                    connection,
                    uid=uid,
                    user_id=user_id,
                    reason="self_hosted_invitation_revoked",
                    owner_lock=proof,
                )
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                "SELECT role, active, status FROM ella_runtime_bindings WHERE user_id = $1 ORDER BY role",
                user_id,
            )
        by_role = {str(row["role"]): row for row in rows}
        assert by_role["user"]["active"] is False
        assert by_role["user"]["status"] == "disabled"
        assert by_role["imessage"]["active"] is True
        assert by_role["imessage"]["status"] == "active"

        async with pool.acquire() as connection:
            async with connection.transaction():
                proof = await authority_advisory_lock.acquire_authority_lock(connection, owner=owner)
                await authority_advisory_lock.verify_self_owner_after_lock(
                    connection,
                    uid=uid,
                    owner=owner,
                    proof=proof,
                )
                await invalidate_self_hosted_authority_on_connection(
                    connection,
                    uid=uid,
                    user_id=user_id,
                    reason="account_deletion_confirmed",
                    owner_lock=proof,
                    include_imessage_binding=True,
                )
        async with pool.acquire() as connection:
            retained = await connection.fetchrow(
                "SELECT active, status FROM ella_runtime_bindings WHERE id = $1",
                runtime.binding_id,
            )
        assert retained["active"] is False
        assert retained["status"] == "disabled"

    asyncio.run(_run_with_database(scenario))


def test_two_owner_identity_collision_has_zero_cross_write():
    async def scenario(pool):
        repository = ImessageEnrollmentRepository(pool)
        user_a, runtime_a = await _seed_owner(pool, uid="imessage-owner-a", ordinal=2)
        user_b, runtime_b = await _seed_owner(pool, uid="imessage-owner-b", ordinal=3)
        receipt_a = await _grant(repository, uid="imessage-owner-a", ordinal=2)
        receipt_b = await _grant(repository, uid="imessage-owner-b", ordinal=3)
        async with pool.acquire() as connection:
            await connection.execute(
                "UPDATE ella_runtime_targets SET profile_user_id = $2 WHERE id = $1",
                runtime_b.target_id,
                user_a,
            )
        with pytest.raises(ImessageAuthorityError, match="imessage_runtime_unavailable"):
            await repository.prepare_registration(
                uid="imessage-owner-b",
                idempotency_key=uuid.uuid5(uuid.NAMESPACE_URL, "cross-owner-target-attempt"),
                handset_ref_hmac=hashlib.sha256(b"cross-owner-target-handset").hexdigest(),
                consent_receipt_id=receipt_b["id"],
                consent_contract=CONSENT_CONTRACT,
                runtime=runtime_b,
            )
        async with pool.acquire() as connection:
            await connection.execute(
                "UPDATE ella_runtime_targets SET profile_user_id = $2 WHERE id = $1",
                runtime_b.target_id,
                user_b,
            )
        binding_a, code_a, destination_a = await _pending_binding(
            repository,
            uid="imessage-owner-a",
            ordinal=2,
            runtime=runtime_a,
            receipt=receipt_a,
        )
        binding_b, code_b, destination_b = await _pending_binding(
            repository,
            uid="imessage-owner-b",
            ordinal=3,
            runtime=runtime_b,
            receipt=receipt_b,
        )
        shared_line = hashlib.sha256(b"shared-line").hexdigest()
        shared_contact = hashlib.sha256(b"shared-contact").hexdigest()
        await repository.verify_inbound_proof(
            assigned_destination_ref_hmac=destination_a,
            handset_ref_hmac=hashlib.sha256(b"handset-2").hexdigest(),
            line_identity_hmac=shared_line,
            contact_identity_hmac=shared_contact,
            provider_message_ref_hmac=hashlib.sha256(b"message-2").hexdigest(),
            candidate_challenge_hash=_proof_hash(salt=f"{2:032x}", code=code_a),
            consent_contract=CONSENT_CONTRACT,
            runtime=runtime_a,
            now=datetime.now(timezone.utc),
        )

        with pytest.raises(ImessageAuthorityError, match="imessage_identity_already_bound"):
            await repository.verify_inbound_proof(
                assigned_destination_ref_hmac=destination_b,
                handset_ref_hmac=hashlib.sha256(b"handset-3").hexdigest(),
                line_identity_hmac=shared_line,
                contact_identity_hmac=shared_contact,
                provider_message_ref_hmac=hashlib.sha256(b"message-3").hexdigest(),
                candidate_challenge_hash=_proof_hash(salt=f"{3:032x}", code=code_b),
                consent_contract=CONSENT_CONTRACT,
                runtime=runtime_b,
                now=datetime.now(timezone.utc),
            )

        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT id, status, line_identity_hmac, contact_identity_hmac
                FROM ella_imessage_channel_bindings
                WHERE id = ANY($1::uuid[])
                ORDER BY id
                """,
                [binding_a["id"], binding_b["id"]],
            )
        states = {row["id"]: row for row in rows}
        assert states[binding_a["id"]]["status"] == "active"
        assert states[binding_b["id"]]["status"] == "verification_pending"
        assert states[binding_b["id"]]["line_identity_hmac"] is None
        assert states[binding_b["id"]]["contact_identity_hmac"] is None

    asyncio.run(_run_with_database(scenario))


def test_provider_registration_identity_is_unique_without_cross_owner_mutation():
    async def scenario(pool):
        repository = ImessageEnrollmentRepository(pool)
        _, runtime_a = await _seed_owner(pool, uid="imessage-provider-owner-a", ordinal=4)
        _, runtime_b = await _seed_owner(pool, uid="imessage-provider-owner-b", ordinal=5)
        receipt_a = await _grant(repository, uid="imessage-provider-owner-a", ordinal=4)
        receipt_b = await _grant(repository, uid="imessage-provider-owner-b", ordinal=5)
        attempt_a, _ = await repository.prepare_registration(
            uid="imessage-provider-owner-a",
            idempotency_key=uuid.uuid5(uuid.NAMESPACE_URL, "provider-owner-a"),
            handset_ref_hmac=hashlib.sha256(b"provider-handset-a").hexdigest(),
            consent_receipt_id=receipt_a["id"],
            consent_contract=CONSENT_CONTRACT,
            runtime=runtime_a,
        )
        attempt_b, _ = await repository.prepare_registration(
            uid="imessage-provider-owner-b",
            idempotency_key=uuid.uuid5(uuid.NAMESPACE_URL, "provider-owner-b"),
            handset_ref_hmac=hashlib.sha256(b"provider-handset-b").hexdigest(),
            consent_receipt_id=receipt_b["id"],
            consent_contract=CONSENT_CONTRACT,
            runtime=runtime_b,
        )
        shared_registration = hashlib.sha256(b"shared-provider-registration").hexdigest()
        await repository.mark_provider_accepted(
            uid="imessage-provider-owner-a",
            attempt_id=attempt_a["id"],
            provider_registration_ref_hmac=shared_registration,
            assigned_destination_e164="+15555550400",
            assigned_destination_ref_hmac=hashlib.sha256(b"provider-destination-a").hexdigest(),
        )
        with pytest.raises(ImessageAuthorityError, match="imessage_provider_identity_conflict"):
            await repository.mark_provider_accepted(
                uid="imessage-provider-owner-b",
                attempt_id=attempt_b["id"],
                provider_registration_ref_hmac=shared_registration,
                assigned_destination_e164="+15555550500",
                assigned_destination_ref_hmac=hashlib.sha256(b"provider-destination-b").hexdigest(),
            )

        async with pool.acquire() as connection:
            states = await connection.fetch(
                "SELECT id, state, provider_registration_ref_hmac FROM ella_imessage_registration_attempts "
                "WHERE id = ANY($1::uuid[])",
                [attempt_a["id"], attempt_b["id"]],
            )
        by_id = {row["id"]: row for row in states}
        assert by_id[attempt_a["id"]]["state"] == "provider_accepted"
        assert by_id[attempt_b["id"]]["state"] == "prepared"
        assert by_id[attempt_b["id"]]["provider_registration_ref_hmac"] is None

    asyncio.run(_run_with_database(scenario))


def test_runtime_receipt_outbox_fences_model_delivery_consent_and_owners():
    async def scenario(pool):
        enrollment = ImessageEnrollmentRepository(pool)
        runtime_repository = ImessageRuntimeRepository(pool)
        await runtime_repository.assert_schema_ready()

        user_a, runtime_a = await _seed_owner(pool, uid="imessage-runtime-owner-a", ordinal=6)
        user_b, runtime_b = await _seed_owner(pool, uid="imessage-runtime-owner-b", ordinal=7)
        consent_a = await _grant(enrollment, uid="imessage-runtime-owner-a", ordinal=6)
        consent_b = await _grant(enrollment, uid="imessage-runtime-owner-b", ordinal=7)
        binding_a, code_a, destination_a = await _pending_binding(
            enrollment,
            uid="imessage-runtime-owner-a",
            ordinal=6,
            runtime=runtime_a,
            receipt=consent_a,
        )
        binding_b, code_b, destination_b = await _pending_binding(
            enrollment,
            uid="imessage-runtime-owner-b",
            ordinal=7,
            runtime=runtime_b,
            receipt=consent_b,
        )
        line_a = hashlib.sha256(b"runtime-line-a").hexdigest()
        contact_a = hashlib.sha256(b"runtime-contact-a").hexdigest()
        line_b = hashlib.sha256(b"runtime-line-b").hexdigest()
        contact_b = hashlib.sha256(b"runtime-contact-b").hexdigest()
        binding_a = await enrollment.verify_inbound_proof(
            assigned_destination_ref_hmac=destination_a,
            handset_ref_hmac=hashlib.sha256(b"handset-6").hexdigest(),
            line_identity_hmac=line_a,
            contact_identity_hmac=contact_a,
            provider_message_ref_hmac=hashlib.sha256(b"runtime-proof-a").hexdigest(),
            candidate_challenge_hash=_proof_hash(salt=f"{6:032x}", code=code_a),
            consent_contract=CONSENT_CONTRACT,
            runtime=runtime_a,
            now=datetime.now(timezone.utc),
        )
        await enrollment.verify_inbound_proof(
            assigned_destination_ref_hmac=destination_b,
            handset_ref_hmac=hashlib.sha256(b"handset-7").hexdigest(),
            line_identity_hmac=line_b,
            contact_identity_hmac=contact_b,
            provider_message_ref_hmac=hashlib.sha256(b"runtime-proof-b").hexdigest(),
            candidate_challenge_hash=_proof_hash(salt=f"{7:032x}", code=code_b),
            consent_contract=CONSENT_CONTRACT,
            runtime=runtime_b,
            now=datetime.now(timezone.utc),
        )

        authority_a = await runtime_repository.resolve_binding(
            line_identity_hmac=line_a,
            contact_identity_hmac=contact_a,
        )
        runtime_authority_a = await _runtime_authority(
            pool,
            uid="imessage-runtime-owner-a",
            ordinal=6,
            runtime=runtime_a,
        )
        assert authority_a["omi_uid"] == "imessage-runtime-owner-a"
        assert (
            await runtime_repository.resolve_binding(
                line_identity_hmac=line_a,
                contact_identity_hmac=contact_b,
            )
            is None
        )
        connection_key = hashlib.sha256(b"runtime-connection-a").hexdigest()
        async with pool.acquire() as connection:
            await connection.execute(
                "UPDATE ella_runtime_bindings SET profile_user_id = $2 WHERE id = $1",
                runtime_a.binding_id,
                user_b,
            )
        with pytest.raises(ImessageRuntimeRepositoryError, match="imessage_transport_authority_changed"):
            await runtime_repository.record_heartbeat(
                binding_id=str(authority_a["id"]),
                generation=int(authority_a["generation"]),
                connection_ref_hmac=connection_key,
                authority=runtime_authority_a,
            )
        async with pool.acquire() as connection:
            await connection.execute(
                "UPDATE ella_runtime_bindings SET profile_user_id = $2 WHERE id = $1",
                runtime_a.binding_id,
                user_a,
            )
        await runtime_repository.record_heartbeat(
            binding_id=str(authority_a["id"]),
            generation=int(authority_a["generation"]),
            connection_ref_hmac=connection_key,
            authority=runtime_authority_a,
        )
        authority_a = await runtime_repository.resolve_binding(
            line_identity_hmac=line_a,
            contact_identity_hmac=contact_a,
        )
        async with pool.acquire() as connection:
            await connection.execute(
                "UPDATE ella_imessage_consent_authority SET decision = 'revoked' WHERE user_id = $1",
                user_a,
            )
        with pytest.raises(ImessageRuntimeRepositoryError, match="imessage_authority_changed"):
            await runtime_repository.claim_message(
                binding=authority_a,
                inbound_provider_ref_hmac=hashlib.sha256(b"runtime-message-denied").hexdigest(),
                inbound_payload_sha256=hashlib.sha256(b"runtime-payload-denied").hexdigest(),
                message_text="content-free denied test",
                occurred_at=datetime.now(timezone.utc),
                lease_seconds=60,
                authority=runtime_authority_a,
            )
        async with pool.acquire() as connection:
            assert (
                await connection.fetchval(
                    "SELECT COUNT(*) FROM ella_imessage_message_receipts WHERE user_id = $1",
                    user_a,
                )
                == 0
            )
            await connection.execute(
                "UPDATE ella_imessage_consent_authority SET decision = 'granted' WHERE user_id = $1",
                user_a,
            )
            await connection.execute(
                "UPDATE ella_runtime_targets SET entitlement_revision = 5 WHERE id = $1",
                runtime_a.target_id,
            )
        with pytest.raises(ImessageRuntimeRepositoryError, match="imessage_authority_changed"):
            await runtime_repository.claim_message(
                binding=authority_a,
                inbound_provider_ref_hmac=hashlib.sha256(b"runtime-message-entitlement-drift").hexdigest(),
                inbound_payload_sha256=hashlib.sha256(b"runtime-payload-entitlement-drift").hexdigest(),
                message_text="content-free entitlement drift",
                occurred_at=datetime.now(timezone.utc),
                lease_seconds=60,
                authority=runtime_authority_a,
            )
        async with pool.acquire() as connection:
            assert (
                await connection.fetchval(
                    "SELECT COUNT(*) FROM ella_imessage_message_receipts WHERE user_id = $1",
                    user_a,
                )
                == 0
            )
            await connection.execute(
                "UPDATE ella_runtime_targets SET entitlement_revision = 4 WHERE id = $1",
                runtime_a.target_id,
            )
        receipt = await runtime_repository.claim_message(
            binding=authority_a,
            inbound_provider_ref_hmac=hashlib.sha256(b"runtime-message-a").hexdigest(),
            inbound_payload_sha256=hashlib.sha256(b"runtime-payload-a").hexdigest(),
            message_text="content-free runtime test",
            occurred_at=datetime.now(timezone.utc),
            lease_seconds=60,
            authority=runtime_authority_a,
        )
        assert receipt["acquired"] is True
        async with pool.acquire() as connection:
            await connection.execute(
                "UPDATE ella_imessage_consent_authority SET decision = 'revoked' WHERE user_id = $1",
                user_a,
            )
        with pytest.raises(ImessageRuntimeRepositoryError, match="imessage_message_claim_conflict"):
            await runtime_repository.mark_model_started(
                receipt_id=str(receipt["id"]),
                lease_token=str(receipt["lease_token"]),
                authority=runtime_authority_a,
            )
        async with pool.acquire() as connection:
            state = await connection.fetchrow(
                "SELECT status, model_started FROM ella_imessage_message_receipts WHERE id = $1",
                receipt["id"],
            )
            assert dict(state) == {"status": "claimed", "model_started": False}
            await connection.execute(
                "UPDATE ella_imessage_consent_authority SET decision = 'granted' WHERE user_id = $1",
                user_a,
            )
            await connection.execute(
                "UPDATE ella_runtime_bindings SET account_user_id = $2 WHERE id = $1",
                runtime_a.binding_id,
                user_b,
            )
        with pytest.raises(ImessageRuntimeRepositoryError, match="imessage_message_claim_conflict"):
            await runtime_repository.mark_model_started(
                receipt_id=str(receipt["id"]),
                lease_token=str(receipt["lease_token"]),
                authority=runtime_authority_a,
            )
        async with pool.acquire() as connection:
            state = await connection.fetchrow(
                "SELECT status, model_started FROM ella_imessage_message_receipts WHERE id = $1",
                receipt["id"],
            )
            assert dict(state) == {"status": "claimed", "model_started": False}
            await connection.execute(
                "UPDATE ella_runtime_bindings SET account_user_id = $2 WHERE id = $1",
                runtime_a.binding_id,
                user_a,
            )
        await runtime_repository.mark_model_started(
            receipt_id=str(receipt["id"]),
            lease_token=str(receipt["lease_token"]),
            authority=runtime_authority_a,
        )
        completed = await runtime_repository.complete_model(
            receipt_id=str(receipt["id"]),
            lease_token=str(receipt["lease_token"]),
            canonical_inbound_event_id="imessage:test:user",
            canonical_outbound_event_id="imessage:test:assistant",
            outbound_text="content-free reply",
            authority=runtime_authority_a,
        )
        assert completed["status"] == "awaiting_delivery"
        async with pool.acquire() as connection:
            await connection.execute(
                "UPDATE ella_runtime_targets SET entitlement_revision = 5 WHERE id = $1",
                runtime_a.target_id,
            )
        with pytest.raises(ImessageRuntimeRepositoryError, match="imessage_delivery_authority_changed"):
            await runtime_repository.start_delivery(
                receipt_id=str(receipt["id"]),
                delivery_idempotency_key=str(receipt["delivery_idempotency_key"]),
                binding_id=str(binding_a["id"]),
                generation=int(binding_a["generation"]),
                connection_ref_hmac=connection_key,
                authority=runtime_authority_a,
            )
        async with pool.acquire() as connection:
            assert (
                await connection.fetchval(
                    "SELECT status FROM ella_imessage_message_receipts WHERE id = $1",
                    receipt["id"],
                )
                == "awaiting_delivery"
            )
            await connection.execute(
                "UPDATE ella_runtime_targets SET entitlement_revision = 4 WHERE id = $1",
                runtime_a.target_id,
            )
        started = await runtime_repository.start_delivery(
            receipt_id=str(receipt["id"]),
            delivery_idempotency_key=str(receipt["delivery_idempotency_key"]),
            binding_id=str(binding_a["id"]),
            generation=int(binding_a["generation"]),
            connection_ref_hmac=connection_key,
            authority=runtime_authority_a,
        )
        assert started["status"] == "sending"
        with pytest.raises(ImessageRuntimeRepositoryError, match="imessage_delivery_outcome_uncertain"):
            await runtime_repository.start_delivery(
                receipt_id=str(receipt["id"]),
                delivery_idempotency_key=str(receipt["delivery_idempotency_key"]),
                binding_id=str(binding_a["id"]),
                generation=int(binding_a["generation"]),
                connection_ref_hmac=connection_key,
                authority=runtime_authority_a,
            )
        async with pool.acquire() as connection:
            await connection.execute(
                "UPDATE ella_imessage_consent_authority SET decision = 'revoked' WHERE user_id = $1",
                user_a,
            )
            await connection.execute(
                "UPDATE ella_runtime_bindings SET account_user_id = $2 WHERE id = $1",
                runtime_a.binding_id,
                user_b,
            )
        outbound_ref = hashlib.sha256(b"runtime-outbound-a").hexdigest()
        delivered = await runtime_repository.acknowledge_delivery(
            receipt_id=str(receipt["id"]),
            delivery_idempotency_key=str(receipt["delivery_idempotency_key"]),
            outbound_provider_ref_hmac=outbound_ref,
            binding_generation=int(binding_a["generation"]),
            line_identity_hmac=line_a,
            contact_identity_hmac=contact_a,
            connection_ref_hmac=connection_key,
        )
        duplicate_ack = await runtime_repository.acknowledge_delivery(
            receipt_id=str(receipt["id"]),
            delivery_idempotency_key=str(receipt["delivery_idempotency_key"]),
            outbound_provider_ref_hmac=outbound_ref,
            binding_generation=int(binding_a["generation"]),
            line_identity_hmac=line_a,
            contact_identity_hmac=contact_a,
            connection_ref_hmac=connection_key,
        )
        assert delivered["status"] == duplicate_ack["status"] == "delivered"
        async with pool.acquire() as connection:
            await connection.execute(
                "UPDATE ella_imessage_consent_authority SET decision = 'granted' WHERE user_id = $1",
                user_a,
            )
            await connection.execute(
                "UPDATE ella_runtime_bindings SET account_user_id = $2 WHERE id = $1",
                runtime_a.binding_id,
                user_a,
            )

        uncertain_delivery = await runtime_repository.claim_message(
            binding=authority_a,
            inbound_provider_ref_hmac=hashlib.sha256(b"runtime-message-delivery-uncertain").hexdigest(),
            inbound_payload_sha256=hashlib.sha256(b"runtime-payload-delivery-uncertain").hexdigest(),
            message_text="content-free uncertain delivery test",
            occurred_at=datetime.now(timezone.utc),
            lease_seconds=60,
            authority=runtime_authority_a,
        )
        await runtime_repository.mark_model_started(
            receipt_id=str(uncertain_delivery["id"]),
            lease_token=str(uncertain_delivery["lease_token"]),
            authority=runtime_authority_a,
        )
        uncertain_delivery = await runtime_repository.complete_model(
            receipt_id=str(uncertain_delivery["id"]),
            lease_token=str(uncertain_delivery["lease_token"]),
            canonical_inbound_event_id="imessage:test:uncertain:user",
            canonical_outbound_event_id="imessage:test:uncertain:assistant",
            outbound_text="content-free uncertain reply",
            authority=runtime_authority_a,
        )
        await runtime_repository.start_delivery(
            receipt_id=str(uncertain_delivery["id"]),
            delivery_idempotency_key=str(uncertain_delivery["delivery_idempotency_key"]),
            binding_id=str(binding_a["id"]),
            generation=int(binding_a["generation"]),
            connection_ref_hmac=connection_key,
            authority=runtime_authority_a,
        )
        with pytest.raises(ImessageRuntimeRepositoryError, match="imessage_delivery_uncertain_conflict"):
            await runtime_repository.mark_delivery_uncertain(
                receipt_id=str(uncertain_delivery["id"]),
                delivery_idempotency_key=str(uncertain_delivery["delivery_idempotency_key"]),
                binding_generation=int(binding_a["generation"]),
                line_identity_hmac=line_a,
                contact_identity_hmac=contact_a,
                connection_ref_hmac=hashlib.sha256(b"runtime-connection-after-restart").hexdigest(),
                error_code="provider_outcome_unconfirmed",
            )
        async with pool.acquire() as connection:
            await connection.execute(
                "UPDATE ella_imessage_consent_authority SET decision = 'revoked' WHERE user_id = $1",
                user_a,
            )
            await connection.execute(
                "UPDATE ella_runtime_bindings SET profile_user_id = $2 WHERE id = $1",
                runtime_a.binding_id,
                user_b,
            )
        uncertain = await runtime_repository.mark_delivery_uncertain(
            receipt_id=str(uncertain_delivery["id"]),
            delivery_idempotency_key=str(uncertain_delivery["delivery_idempotency_key"]),
            binding_generation=int(binding_a["generation"]),
            line_identity_hmac=line_a,
            contact_identity_hmac=contact_a,
            connection_ref_hmac=connection_key,
            error_code="provider_outcome_unconfirmed",
        )
        duplicate_uncertain = await runtime_repository.mark_delivery_uncertain(
            receipt_id=str(uncertain_delivery["id"]),
            delivery_idempotency_key=str(uncertain_delivery["delivery_idempotency_key"]),
            binding_generation=int(binding_a["generation"]),
            line_identity_hmac=line_a,
            contact_identity_hmac=contact_a,
            connection_ref_hmac=connection_key,
            error_code="provider_outcome_unconfirmed",
        )
        assert uncertain["status"] == duplicate_uncertain["status"] == "uncertain"
        async with pool.acquire() as connection:
            await connection.execute(
                "UPDATE ella_imessage_consent_authority SET decision = 'granted' WHERE user_id = $1",
                user_a,
            )
            await connection.execute(
                "UPDATE ella_runtime_bindings SET profile_user_id = $2 WHERE id = $1",
                runtime_a.binding_id,
                user_a,
            )

        stale = await runtime_repository.claim_message(
            binding=authority_a,
            inbound_provider_ref_hmac=hashlib.sha256(b"runtime-message-stale").hexdigest(),
            inbound_payload_sha256=hashlib.sha256(b"runtime-payload-stale").hexdigest(),
            message_text="content-free stale test",
            occurred_at=datetime.now(timezone.utc),
            lease_seconds=60,
            authority=runtime_authority_a,
        )
        await runtime_repository.mark_model_started(
            receipt_id=str(stale["id"]),
            lease_token=str(stale["lease_token"]),
            authority=runtime_authority_a,
        )
        async with pool.acquire() as connection:
            await connection.execute(
                "UPDATE ella_imessage_message_receipts SET lease_expires_at = CURRENT_TIMESTAMP - INTERVAL '1 second' WHERE id = $1",
                stale["id"],
            )
        replay = await runtime_repository.claim_message(
            binding=authority_a,
            inbound_provider_ref_hmac=hashlib.sha256(b"runtime-message-stale").hexdigest(),
            inbound_payload_sha256=hashlib.sha256(b"runtime-payload-stale").hexdigest(),
            message_text="content-free stale test",
            occurred_at=datetime.now(timezone.utc),
            lease_seconds=60,
            authority=runtime_authority_a,
        )
        assert replay["status"] == "uncertain"
        assert replay["acquired"] is False
        assert replay["reconciliation_status"] == "manual_required"

        await enrollment.submit_consent(
            uid="imessage-runtime-owner-a",
            submission=ImessageConsentInput(
                request_id=uuid.uuid5(uuid.NAMESPACE_URL, "runtime-revoke-a"),
                decision="revoked",
                policy_version=POLICY,
                processor_set_hash=PROCESSOR_HASH,
                scope_version=SCOPE,
                scope_hash=SCOPE_HASH,
                app_version="1.0",
                build_number="1",
            ),
        )
        assert (
            await runtime_repository.resolve_binding(
                line_identity_hmac=line_a,
                contact_identity_hmac=contact_a,
            )
            is None
        )

        async with pool.acquire() as connection:
            owner_counts = await connection.fetch(
                """
                SELECT user_id, COUNT(*) AS count
                FROM ella_imessage_message_receipts
                GROUP BY user_id
                """
            )
        assert {row["user_id"]: int(row["count"]) for row in owner_counts} == {user_a: 3}

    asyncio.run(_run_with_database(scenario))


def test_retained_owner_authority_is_targetless_and_fails_closed_if_a_target_appears():
    async def scenario(pool):
        enrollment = ImessageEnrollmentRepository(pool)
        runtime_repository = ImessageRuntimeRepository(pool)
        await enrollment.assert_schema_ready()
        await runtime_repository.assert_schema_ready()

        uid = "imessage-retained-owner"
        user_id, runtime = await _seed_retained_owner(pool, uid=uid, ordinal=16)
        consent = await _grant(enrollment, uid=uid, ordinal=16)
        pending, code, destination = await _pending_binding(
            enrollment,
            uid=uid,
            ordinal=16,
            runtime=runtime,
            receipt=consent,
        )
        line = hashlib.sha256(b"retained-line").hexdigest()
        contact = hashlib.sha256(b"retained-contact").hexdigest()
        binding = await enrollment.verify_inbound_proof(
            assigned_destination_ref_hmac=destination,
            handset_ref_hmac=hashlib.sha256(b"handset-16").hexdigest(),
            line_identity_hmac=line,
            contact_identity_hmac=contact,
            provider_message_ref_hmac=hashlib.sha256(b"retained-proof").hexdigest(),
            candidate_challenge_hash=_proof_hash(salt=f"{16:032x}", code=code),
            consent_contract=CONSENT_CONTRACT,
            runtime=runtime,
            now=datetime.now(timezone.utc),
        )
        authority = await _runtime_authority(pool, uid=uid, ordinal=16, runtime=runtime)
        resolved = await runtime_repository.resolve_binding(
            line_identity_hmac=line,
            contact_identity_hmac=contact,
        )

        assert pending["runtime_authority_kind"] == "retained_owner"
        assert binding["runtime_authority_kind"] == "retained_owner"
        assert resolved["runtime_authority_kind"] == "retained_owner"
        assert resolved["runtime_target_id"] is None
        assert authority.runtime_authority_kind == "retained_owner"
        async with pool.acquire() as connection:
            binding_roles = await connection.fetch(
                """
                SELECT id, role, active,
                       EXISTS (
                           SELECT 1 FROM ella_runtime_targets target
                           WHERE target.runtime_binding_id = binding.id
                       ) AS has_target
                FROM ella_runtime_bindings binding
                WHERE user_id = $1 AND provider = 'hermes'
                ORDER BY role
                """,
                user_id,
            )
        by_role = {str(row["role"]): row for row in binding_roles}
        assert set(by_role) == {"imessage", "user"}
        assert by_role["imessage"]["id"] == runtime.binding_id
        assert by_role["imessage"]["has_target"] is False
        assert by_role["user"]["id"] != runtime.binding_id
        assert by_role["user"]["active"] is True
        assert by_role["user"]["has_target"] is True
        connection_key = hashlib.sha256(b"retained-connection").hexdigest()
        await runtime_repository.record_heartbeat(
            binding_id=str(binding["id"]),
            generation=int(binding["generation"]),
            connection_ref_hmac=connection_key,
            authority=authority,
        )
        resolved = await runtime_repository.resolve_binding(
            line_identity_hmac=line,
            contact_identity_hmac=contact,
        )
        receipt = await runtime_repository.claim_message(
            binding=resolved,
            inbound_provider_ref_hmac=hashlib.sha256(b"retained-message").hexdigest(),
            inbound_payload_sha256=hashlib.sha256(b"retained-payload").hexdigest(),
            message_text="content-free retained runtime test",
            occurred_at=datetime.now(timezone.utc),
            lease_seconds=60,
            authority=authority,
        )
        assert receipt["runtime_authority_kind"] == "retained_owner"
        assert receipt["runtime_target_id"] is None
        await runtime_repository.mark_model_started(
            receipt_id=str(receipt["id"]),
            lease_token=str(receipt["lease_token"]),
            authority=authority,
        )
        receipt = await runtime_repository.complete_model(
            receipt_id=str(receipt["id"]),
            lease_token=str(receipt["lease_token"]),
            canonical_inbound_event_id="imessage:retained:user",
            canonical_outbound_event_id="imessage:retained:assistant",
            outbound_text="content-free retained reply",
            authority=authority,
        )

        async with pool.acquire() as connection:
            drift_target = await connection.fetchval(
                """
                INSERT INTO ella_runtime_targets (
                    account_user_id, profile_user_id, role, mode, provider,
                    runtime_binding_id, candidate_runtime_instance_id,
                    endpoint_ref, credential_ref, status, policy_version,
                    processor_set_hash, scope_version, scope_hash,
                    entitlement_revision
                ) VALUES (
                    $1, $1, 'user', 'hermes-cloud-chat', 'hermes_cloud',
                    $2, 'unexpected-instance', 'unexpected-endpoint-ref',
                    'unexpected-credential-ref', 'ready', $3, $4, $5, $6, 1
                ) RETURNING id
                """,
                user_id,
                runtime.binding_id,
                POLICY,
                PROCESSOR_HASH,
                SCOPE,
                SCOPE_HASH,
            )

        assert (
            await runtime_repository.resolve_binding(
                line_identity_hmac=line,
                contact_identity_hmac=contact,
            )
            is None
        )
        with pytest.raises(ImessageRuntimeRepositoryError, match="imessage_transport_authority_changed"):
            await runtime_repository.record_heartbeat(
                binding_id=str(binding["id"]),
                generation=int(binding["generation"]),
                connection_ref_hmac=connection_key,
                authority=authority,
            )
        with pytest.raises(ImessageRuntimeRepositoryError, match="imessage_delivery_authority_changed"):
            await runtime_repository.start_delivery(
                receipt_id=str(receipt["id"]),
                delivery_idempotency_key=str(receipt["delivery_idempotency_key"]),
                binding_id=str(binding["id"]),
                generation=int(binding["generation"]),
                connection_ref_hmac=connection_key,
                authority=authority,
            )

        async with pool.acquire() as connection:
            await connection.execute("DELETE FROM ella_runtime_targets WHERE id = $1", drift_target)
        started = await runtime_repository.start_delivery(
            receipt_id=str(receipt["id"]),
            delivery_idempotency_key=str(receipt["delivery_idempotency_key"]),
            binding_id=str(binding["id"]),
            generation=int(binding["generation"]),
            connection_ref_hmac=connection_key,
            authority=authority,
        )
        assert started["status"] == "sending"

    asyncio.run(_run_with_database(scenario))


def test_retained_runtime_transition_serializes_with_concurrent_target_creation():
    async def scenario(pool):
        enrollment = ImessageEnrollmentRepository(pool)
        runtime_repository = ImessageRuntimeRepository(pool)
        uid = "imessage-retained-target-race"
        user_id, runtime = await _seed_retained_owner(pool, uid=uid, ordinal=18)
        consent = await _grant(enrollment, uid=uid, ordinal=18)
        _, code, destination = await _pending_binding(
            enrollment,
            uid=uid,
            ordinal=18,
            runtime=runtime,
            receipt=consent,
        )
        line = hashlib.sha256(b"retained-race-line").hexdigest()
        contact = hashlib.sha256(b"retained-race-contact").hexdigest()
        binding = await enrollment.verify_inbound_proof(
            assigned_destination_ref_hmac=destination,
            handset_ref_hmac=hashlib.sha256(b"handset-18").hexdigest(),
            line_identity_hmac=line,
            contact_identity_hmac=contact,
            provider_message_ref_hmac=hashlib.sha256(b"retained-race-proof").hexdigest(),
            candidate_challenge_hash=_proof_hash(salt=f"{18:032x}", code=code),
            consent_contract=CONSENT_CONTRACT,
            runtime=runtime,
            now=datetime.now(timezone.utc),
        )
        authority = await _runtime_authority(pool, uid=uid, ordinal=18, runtime=runtime)
        owner = authority_advisory_lock.AuthorityOwner.from_values(user_id, user_id)
        async with pool.acquire() as connection:
            original_last_healthy = await connection.fetchval(
                "SELECT last_transport_healthy_at FROM ella_imessage_channel_bindings WHERE id = $1",
                binding["id"],
            )
        heartbeat = None
        async with pool.acquire() as writer:
            async with writer.transaction():
                proof = await authority_advisory_lock.acquire_authority_lock(writer, owner=owner)
                await authority_advisory_lock.verify_self_owner_after_lock(
                    writer,
                    uid=uid,
                    owner=owner,
                    proof=proof,
                )
                heartbeat = asyncio.create_task(
                    runtime_repository.record_heartbeat(
                        binding_id=str(binding["id"]),
                        generation=int(binding["generation"]),
                        connection_ref_hmac=hashlib.sha256(b"retained-race-connection").hexdigest(),
                        authority=authority,
                    )
                )
                done, pending = await asyncio.wait({heartbeat}, timeout=0.1)
                assert done == set()
                assert pending == {heartbeat}
                await writer.execute(
                    """
                    INSERT INTO ella_runtime_targets (
                        account_user_id, profile_user_id, role, mode, provider,
                        runtime_binding_id, candidate_runtime_instance_id,
                        endpoint_ref, credential_ref, status, policy_version,
                        processor_set_hash, scope_version, scope_hash,
                        entitlement_revision
                    ) VALUES (
                        $1, $1, 'user', 'hermes-cloud-chat', 'hermes_cloud',
                        $2, 'concurrent-instance', 'concurrent-endpoint-ref',
                        'concurrent-credential-ref', 'ready', $3, $4, $5, $6, 1
                    )
                    """,
                    user_id,
                    runtime.binding_id,
                    POLICY,
                    PROCESSOR_HASH,
                    SCOPE,
                    SCOPE_HASH,
                )

        assert heartbeat is not None
        with pytest.raises(ImessageRuntimeRepositoryError, match="imessage_transport_authority_changed"):
            await heartbeat
        async with pool.acquire() as connection:
            last_healthy = await connection.fetchval(
                "SELECT last_transport_healthy_at FROM ella_imessage_channel_bindings WHERE id = $1",
                binding["id"],
            )
        assert last_healthy == original_last_healthy

    asyncio.run(_run_with_database(scenario))


def test_expired_pending_binding_is_atomically_retired_and_replacement_can_start():
    async def scenario(pool):
        repository = ImessageEnrollmentRepository(pool)
        uid = "imessage-expired-owner"
        _, runtime = await _seed_owner(pool, uid=uid, ordinal=6)
        receipt = await _grant(repository, uid=uid, ordinal=6)
        binding, _, destination_hmac = await _pending_binding(
            repository,
            uid=uid,
            ordinal=6,
            runtime=runtime,
            receipt=receipt,
        )
        expired_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        async with pool.acquire() as connection:
            await connection.execute(
                "UPDATE ella_imessage_channel_bindings SET challenge_expires_at = $2 WHERE id = $1",
                binding["id"],
                expired_at,
            )

        retired = await repository.retire_expired_pending_binding(
            uid=uid,
            binding_id=binding["id"],
            now=datetime.now(timezone.utc),
        )

        assert retired["status"] == "quarantined"
        with pytest.raises(ImessageAuthorityError, match="imessage_proof_binding_not_found"):
            await repository.resolve_proof_authority(
                assigned_destination_ref_hmac=destination_hmac,
                handset_ref_hmac=hashlib.sha256(b"handset-6").hexdigest(),
            )
        replacement, created = await repository.prepare_registration(
            uid=uid,
            idempotency_key=uuid.uuid5(uuid.NAMESPACE_URL, "replacement-enrollment-6"),
            handset_ref_hmac=hashlib.sha256(b"replacement-handset-6").hexdigest(),
            consent_receipt_id=receipt["id"],
            consent_contract=CONSENT_CONTRACT,
            runtime=runtime,
        )
        assert created is True
        assert replacement["state"] == "prepared"

        async with pool.acquire() as connection:
            state = await connection.fetchrow(
                """
                SELECT b.status, b.revision, a.state AS attempt_state, a.error_code
                FROM ella_imessage_channel_bindings b
                JOIN ella_imessage_registration_attempts a ON a.id = b.registration_attempt_id
                WHERE b.id = $1
                """,
                binding["id"],
            )
        assert state["status"] == "quarantined"
        assert int(state["revision"]) == int(binding["revision"]) + 1
        assert state["attempt_state"] == "quarantined"
        assert state["error_code"] == "imessage_proof_expired"

    asyncio.run(_run_with_database(scenario))


def test_revoke_returns_exact_provider_request_for_idempotent_local_cleanup():
    async def scenario(pool):
        repository = ImessageEnrollmentRepository(pool)
        uid = "imessage-cleanup-owner"
        _, runtime = await _seed_owner(pool, uid=uid, ordinal=8)
        receipt = await _grant(repository, uid=uid, ordinal=8)
        binding, _, _ = await _pending_binding(
            repository,
            uid=uid,
            ordinal=8,
            runtime=runtime,
            receipt=receipt,
        )
        revoke_key = uuid.uuid5(uuid.NAMESPACE_URL, "imessage-cleanup-revoke")
        revoked = await repository.revoke_binding(
            uid=uid,
            expected_generation=int(binding["generation"]),
            idempotency_key=revoke_key,
        )
        duplicate = await repository.revoke_binding(
            uid=uid,
            expected_generation=int(binding["generation"]),
            idempotency_key=revoke_key,
        )
        async with pool.acquire() as connection:
            expected_request_id = await connection.fetchval(
                "SELECT provider_request_id FROM ella_imessage_registration_attempts WHERE id = $1",
                binding["registration_attempt_id"],
            )
        assert revoked["status"] == duplicate["status"] == "revoked"
        assert revoked["provider_request_id"] == duplicate["provider_request_id"] == expected_request_id

    asyncio.run(_run_with_database(scenario))


def test_unresolved_provider_acceptance_blocks_competing_registration_graph():
    async def scenario(pool):
        repository = ImessageEnrollmentRepository(pool)
        uid = "imessage-unresolved-owner"
        _, runtime = await _seed_owner(pool, uid=uid, ordinal=7)
        receipt = await _grant(repository, uid=uid, ordinal=7)
        attempt, created = await repository.prepare_registration(
            uid=uid,
            idempotency_key=uuid.uuid5(uuid.NAMESPACE_URL, "unresolved-enrollment-7"),
            handset_ref_hmac=hashlib.sha256(b"unresolved-handset-7").hexdigest(),
            consent_receipt_id=receipt["id"],
            consent_contract=CONSENT_CONTRACT,
            runtime=runtime,
        )
        assert created is True
        accepted = await repository.mark_provider_accepted(
            uid=uid,
            attempt_id=attempt["id"],
            provider_registration_ref_hmac=hashlib.sha256(b"unresolved-registration-7").hexdigest(),
            assigned_destination_e164="+15555550700",
            assigned_destination_ref_hmac=hashlib.sha256(b"unresolved-destination-7").hexdigest(),
        )
        assert accepted["state"] == "provider_accepted"

        with pytest.raises(ImessageAuthorityError, match="imessage_registration_manual_reconciliation_required"):
            await repository.prepare_registration(
                uid=uid,
                idempotency_key=uuid.uuid5(uuid.NAMESPACE_URL, "competing-enrollment-7"),
                handset_ref_hmac=hashlib.sha256(b"competing-handset-7").hexdigest(),
                consent_receipt_id=receipt["id"],
                consent_contract=CONSENT_CONTRACT,
                runtime=runtime,
            )

        async with pool.acquire() as connection:
            attempts = await connection.fetch(
                "SELECT id, state FROM ella_imessage_registration_attempts WHERE user_id = $1",
                runtime.account_user_id,
            )
        assert [(row["id"], row["state"]) for row in attempts] == [(attempt["id"], "provider_accepted")]

    asyncio.run(_run_with_database(scenario))


def test_pre_send_reconciliation_is_exact_terminal_and_never_releases_text():
    async def scenario(pool):
        enrollment = ImessageEnrollmentRepository(pool)
        runtime_repository = ImessageRuntimeRepository(pool)
        uid = "imessage-reconcile-owner"
        _, runtime = await _seed_owner(pool, uid=uid, ordinal=9)
        consent = await _grant(enrollment, uid=uid, ordinal=9)
        binding, code, destination_hmac = await _pending_binding(
            enrollment,
            uid=uid,
            ordinal=9,
            runtime=runtime,
            receipt=consent,
        )
        line_hmac = hashlib.sha256(b"reconcile-line").hexdigest()
        contact_hmac = hashlib.sha256(b"reconcile-contact").hexdigest()
        connection_hmac = hashlib.sha256(b"reconcile-connection").hexdigest()
        await enrollment.verify_inbound_proof(
            assigned_destination_ref_hmac=destination_hmac,
            handset_ref_hmac=hashlib.sha256(b"handset-9").hexdigest(),
            line_identity_hmac=line_hmac,
            contact_identity_hmac=contact_hmac,
            provider_message_ref_hmac=hashlib.sha256(b"reconcile-proof-message").hexdigest(),
            candidate_challenge_hash=_proof_hash(salt=f"{9:032x}", code=code),
            consent_contract=CONSENT_CONTRACT,
            runtime=runtime,
            now=datetime.now(timezone.utc),
        )
        authority = await _runtime_authority(pool, uid=uid, ordinal=9, runtime=runtime)
        active = await runtime_repository.resolve_binding(
            line_identity_hmac=line_hmac,
            contact_identity_hmac=contact_hmac,
        )
        assert active is not None
        await runtime_repository.record_heartbeat(
            binding_id=str(active["id"]),
            generation=int(active["generation"]),
            connection_ref_hmac=connection_hmac,
            authority=authority,
        )

        async def completed_receipt(label: str):
            claimed = await runtime_repository.claim_message(
                binding=active,
                inbound_provider_ref_hmac=hashlib.sha256(f"{label}-message".encode()).hexdigest(),
                inbound_payload_sha256=hashlib.sha256(f"{label}-payload".encode()).hexdigest(),
                message_text="content-free reconciliation test",
                occurred_at=datetime.now(timezone.utc),
                lease_seconds=60,
                authority=authority,
            )
            await runtime_repository.mark_model_started(
                receipt_id=str(claimed["id"]),
                lease_token=str(claimed["lease_token"]),
                authority=authority,
            )
            return await runtime_repository.complete_model(
                receipt_id=str(claimed["id"]),
                lease_token=str(claimed["lease_token"]),
                canonical_inbound_event_id=f"imessage:{label}:user",
                canonical_outbound_event_id=f"imessage:{label}:assistant",
                outbound_text="content-free reply",
                authority=authority,
            )

        pre_send = await completed_receipt("pre-send")
        quarantined = await runtime_repository.reconcile_pre_send_delivery(
            receipt_id=str(pre_send["id"]),
            delivery_idempotency_key=str(pre_send["delivery_idempotency_key"]),
            binding_generation=int(active["generation"]),
            line_identity_hmac=line_hmac,
            contact_identity_hmac=contact_hmac,
            connection_ref_hmac=connection_hmac,
        )
        assert quarantined["status"] == "quarantined"
        assert quarantined["reconciliation_status"] == "manual_required"

        started = await completed_receipt("started")
        await runtime_repository.start_delivery(
            receipt_id=str(started["id"]),
            delivery_idempotency_key=str(started["delivery_idempotency_key"]),
            binding_id=str(active["id"]),
            generation=int(active["generation"]),
            connection_ref_hmac=connection_hmac,
            authority=authority,
        )
        with pytest.raises(ImessageRuntimeRepositoryError, match="imessage_delivery_reconcile_conflict"):
            await runtime_repository.reconcile_pre_send_delivery(
                receipt_id=str(started["id"]),
                delivery_idempotency_key=str(started["delivery_idempotency_key"]),
                binding_generation=int(active["generation"]),
                line_identity_hmac=line_hmac,
                contact_identity_hmac=contact_hmac,
                connection_ref_hmac=hashlib.sha256(b"wrong-connection").hexdigest(),
            )
        uncertain = await runtime_repository.reconcile_pre_send_delivery(
            receipt_id=str(started["id"]),
            delivery_idempotency_key=str(started["delivery_idempotency_key"]),
            binding_generation=int(active["generation"]),
            line_identity_hmac=line_hmac,
            contact_identity_hmac=contact_hmac,
            connection_ref_hmac=connection_hmac,
        )
        duplicate = await runtime_repository.reconcile_pre_send_delivery(
            receipt_id=str(started["id"]),
            delivery_idempotency_key=str(started["delivery_idempotency_key"]),
            binding_generation=int(active["generation"]),
            line_identity_hmac=line_hmac,
            contact_identity_hmac=contact_hmac,
            connection_ref_hmac=connection_hmac,
        )
        assert uncertain["status"] == duplicate["status"] == "uncertain"
        assert uncertain["outbound_text"] == "content-free reply"

    asyncio.run(_run_with_database(scenario))


def test_account_deletion_fence_quarantines_owner_and_preserves_unrelated_registration():
    async def scenario(pool):
        repository = ImessageEnrollmentRepository(pool)
        runtime_repository = ImessageRuntimeRepository(pool)
        _, runtime_a = await _seed_owner(pool, uid="imessage-delete-owner-a", ordinal=10)
        _, runtime_b = await _seed_owner(pool, uid="imessage-delete-owner-b", ordinal=11)
        consent_a = await _grant(repository, uid="imessage-delete-owner-a", ordinal=10)
        consent_b = await _grant(repository, uid="imessage-delete-owner-b", ordinal=11)
        binding_a, code_a, destination_a = await _pending_binding(
            repository,
            uid="imessage-delete-owner-a",
            ordinal=10,
            runtime=runtime_a,
            receipt=consent_a,
        )
        binding_b, code_b, destination_b = await _pending_binding(
            repository,
            uid="imessage-delete-owner-b",
            ordinal=11,
            runtime=runtime_b,
            receipt=consent_b,
        )
        line_a = hashlib.sha256(b"delete-line-a").hexdigest()
        contact_a = hashlib.sha256(b"delete-contact-a").hexdigest()
        line_b = hashlib.sha256(b"delete-line-b").hexdigest()
        contact_b = hashlib.sha256(b"delete-contact-b").hexdigest()
        for ordinal, runtime, binding, code, destination, line, contact in (
            (10, runtime_a, binding_a, code_a, destination_a, line_a, contact_a),
            (11, runtime_b, binding_b, code_b, destination_b, line_b, contact_b),
        ):
            await repository.verify_inbound_proof(
                assigned_destination_ref_hmac=destination,
                handset_ref_hmac=hashlib.sha256(f"handset-{ordinal}".encode()).hexdigest(),
                line_identity_hmac=line,
                contact_identity_hmac=contact,
                provider_message_ref_hmac=hashlib.sha256(f"delete-proof-{ordinal}".encode()).hexdigest(),
                candidate_challenge_hash=_proof_hash(salt=f"{ordinal:032x}", code=code),
                consent_contract=CONSENT_CONTRACT,
                runtime=runtime,
                now=datetime.now(timezone.utc),
            )
        active_a = await runtime_repository.resolve_binding(
            line_identity_hmac=line_a,
            contact_identity_hmac=contact_a,
        )
        authority_a = await _runtime_authority(
            pool,
            uid="imessage-delete-owner-a",
            ordinal=10,
            runtime=runtime_a,
        )
        claimed = await runtime_repository.claim_message(
            binding=active_a,
            inbound_provider_ref_hmac=hashlib.sha256(b"delete-message-a").hexdigest(),
            inbound_payload_sha256=hashlib.sha256(b"delete-payload-a").hexdigest(),
            message_text="content-free deletion fence test",
            occurred_at=datetime.now(timezone.utc),
            lease_seconds=60,
            authority=authority_a,
        )

        request_id = uuid.uuid5(uuid.NAMESPACE_URL, "imessage-account-delete-a")
        fence = await repository.begin_account_deletion_cleanup(
            uid="imessage-delete-owner-a",
            request_id=request_id,
        )
        duplicate = await repository.begin_account_deletion_cleanup(
            uid="imessage-delete-owner-a",
            request_id=uuid.uuid4(),
        )
        assert fence["request_id"] == duplicate["request_id"] == request_id
        assert fence["state"] == "pending"
        assert len(fence["provider_request_ids"]) == 1
        assert (
            await runtime_repository.resolve_binding(
                line_identity_hmac=line_a,
                contact_identity_hmac=contact_a,
            )
            is None
        )
        assert (
            await runtime_repository.resolve_binding(
                line_identity_hmac=line_b,
                contact_identity_hmac=contact_b,
            )
            is not None
        )
        with pytest.raises(ImessageAuthorityError, match="imessage_owner_not_active"):
            await repository.prepare_registration(
                uid="imessage-delete-owner-a",
                idempotency_key=uuid.uuid4(),
                handset_ref_hmac=hashlib.sha256(b"delete-retry-handset").hexdigest(),
                consent_receipt_id=consent_a["id"],
                consent_contract=CONSENT_CONTRACT,
                runtime=runtime_a,
            )
        async with pool.acquire() as connection:
            assert (
                await connection.fetchval(
                    "SELECT status FROM ella_imessage_message_receipts WHERE id = $1",
                    claimed["id"],
                )
                == "quarantined"
            )
            assert (
                await connection.fetchval(
                    "SELECT status FROM ella_imessage_channel_bindings WHERE id = $1",
                    binding_b["id"],
                )
                == "active"
            )
        cleaned = await repository.complete_account_deletion_cleanup(
            uid="imessage-delete-owner-a",
            request_id=request_id,
        )
        assert cleaned["state"] == "cleaned"

    asyncio.run(_run_with_database(scenario))
