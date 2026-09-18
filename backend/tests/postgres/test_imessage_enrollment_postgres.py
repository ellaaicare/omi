import asyncio
import hashlib
import hmac
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg
import pytest

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


async def _run_with_database(scenario):
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
            for name in MIGRATION_CHAIN[2:]:
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
        binding_id=binding_id,
        target_id=target_id,
        authority_digest=hashlib.sha256(f"runtime-{ordinal}".encode()).hexdigest(),
        binding_revision=3,
        entitlement_revision=4,
        account_user_id=user_id,
        profile_user_id=user_id,
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
        runtime_binding_revision=runtime.binding_revision,
        runtime_target_entitlement_revision=runtime.entitlement_revision,
        runtime_target_updated_at=target_updated_at,
        runtime_authority_digest=runtime.authority_digest,
        runtime_agent_id=f"imessage-agent-{ordinal}",
        runtime_instance_id=None,
        runtime_profile_name=f"imessage-profile-{ordinal}",
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
