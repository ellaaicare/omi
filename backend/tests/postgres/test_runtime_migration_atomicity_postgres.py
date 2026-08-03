import asyncio
import os
import uuid
from pathlib import Path

import asyncpg
import pytest

TEST_DSN = os.getenv("ELLA_TEST_POSTGRES_DSN", "").strip()
MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"

BASE_PROVISIONING_SCHEMA = """
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    omi_uid TEXT UNIQUE,
    email TEXT UNIQUE,
    name TEXT NOT NULL DEFAULT 'Migration User',
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

pytestmark = pytest.mark.skipif(
    not TEST_DSN,
    reason="ELLA_TEST_POSTGRES_DSN is required for migration atomicity tests",
)


async def _assert_failure_rolls_back(filename: str, leaked_relations: tuple[str, ...]) -> None:
    schema = f"migration_failure_{uuid.uuid4().hex}"
    admin = await asyncpg.connect(TEST_DSN)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    failing = await asyncpg.connect(
        TEST_DSN,
        server_settings={"search_path": schema},
    )
    try:
        with pytest.raises(asyncpg.PostgresError):
            await failing.execute((MIGRATIONS / filename).read_text(encoding="utf-8"))
    finally:
        await failing.close()

    verifying = await asyncpg.connect(
        TEST_DSN,
        server_settings={"search_path": schema},
    )
    try:
        for relation in leaked_relations:
            assert await verifying.fetchval("SELECT to_regclass($1)", relation) is None
    finally:
        await verifying.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


@pytest.mark.parametrize(
    ("filename", "leaked_relations"),
    [
        (
            "011_create_invitation_redemption.sql",
            (
                "ella_invitation_capacity_reservations",
                "ella_invitations",
                "ella_invitation_targets",
            ),
        ),
        (
            "012_create_account_profile_runtime_targets.sql",
            (
                "ella_runtime_targets",
                "ella_runtime_session_scopes",
                "ella_runtime_interactions",
            ),
        ),
        (
            "013_create_managed_cloud_consent_authority.sql",
            ("ella_managed_cloud_consent_authority",),
        ),
        (
            "015_add_invitation_allowed_email_hash.sql",
            ("ella_runtime_targets_invitation_target_key",),
        ),
        (
            "017_add_provider_attempt_deletion_fence.sql",
            ("ella_provider_attempts",),
        ),
    ],
)
def test_migration_prerequisite_failure_is_atomic(filename, leaked_relations):
    asyncio.run(_assert_failure_rolls_back(filename, leaked_relations))


def test_migration_015_upgrades_multi_redemption_app_review_history_atomically():
    async def scenario() -> None:
        schema = f"migration_upgrade_{uuid.uuid4().hex}"
        admin = await asyncpg.connect(TEST_DSN)
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        conn = await asyncpg.connect(TEST_DSN, server_settings={"search_path": schema})
        try:
            await conn.execute(BASE_PROVISIONING_SCHEMA)
            for name in (
                "008_create_voice_canary_controls.sql",
                "009_create_hermes_cloud_runtime_pool.sql",
                "010_add_cloud_profile_class.sql",
                "011_create_invitation_redemption.sql",
                "012_create_account_profile_runtime_targets.sql",
                "013_create_managed_cloud_consent_authority.sql",
                "014_add_synthetic_invitation_operator_audit.sql",
            ):
                await conn.execute((MIGRATIONS / name).read_text(encoding="utf-8"))

            user_ids = await conn.fetch(
                """
                INSERT INTO users (omi_uid, email, profile_class)
                VALUES
                    ('review-history-a', 'review-a@example.test', 'synthetic'),
                    ('review-history-b', 'review-b@example.test', 'synthetic')
                RETURNING id, omi_uid
                """
            )
            reservation_id = await conn.fetchval(
                """
                INSERT INTO ella_invitation_capacity_reservations (
                    pool_key, state, reserved_slots
                ) VALUES ('app_review', 'reserved', 2)
                RETURNING id
                """
            )
            invitation_id = await conn.fetchval(
                """
                INSERT INTO ella_invitations (
                    capacity_reservation_id, kind, code_hmac, display_hint,
                    state, delivery_state, usage_mode, max_redemptions,
                    redemption_count, reserved_setup_slots,
                    entitlement_policy_revision, entitlement_policy,
                    required_consent_policy_version,
                    required_consent_processor_set_hash,
                    required_consent_scope_version, required_consent_scope_hash,
                    cohort, exclude_from_product_analytics, first_sent_at
                ) VALUES (
                    $1, 'app_review', $2, 'AB', 'sent', 'sent',
                    'capped_multi_redeem', 20, 2, 2, 'review-history-v1',
                    '{}'::jsonb, 'policy-v1', $3, 'scope-v1', $4,
                    'app_review', TRUE, NOW()
                )
                RETURNING id
                """,
                reservation_id,
                "1" * 64,
                "sha256:" + "2" * 64,
                "sha256:" + "3" * 64,
            )
            for index, user in enumerate(user_ids):
                authority_epoch = await conn.fetchval(
                    """
                    INSERT INTO ella_managed_cloud_consent_authority (
                        user_id, decision, consent_receipt_ref,
                        profile_binding_id, policy_version,
                        processor_set_hash, scope_version, scope_hash
                    ) VALUES ($1, 'granted', $2, $3, 'policy-v1', $4, 'scope-v1', $5)
                    RETURNING authority_epoch
                    """,
                    user["id"],
                    "sha256:" + f"{index + 4}" * 64,
                    f"review-history-binding-{index}",
                    "sha256:" + "2" * 64,
                    "sha256:" + "3" * 64,
                )
                target_id = await conn.fetchval(
                    """
                    INSERT INTO ella_invitation_targets (
                        invitation_id, account_ref_hmac, profile_ref_hmac,
                        required_profile_class, consumed_at
                    ) VALUES ($1, $2, $3, 'synthetic', NOW())
                    RETURNING id
                    """,
                    invitation_id,
                    f"{index + 4}" * 64,
                    f"{index + 6}" * 64,
                )
                await conn.execute(
                    """
                    INSERT INTO voice_entitlements (
                        uid, status, invitation_id, entitlement_policy_revision,
                        consent_policy_version, consent_processor_set_hash,
                        consent_scope_version, consent_scope_hash,
                        consent_authority_epoch
                    ) VALUES ($1, 'active', $2, 'review-history-v1',
                              'policy-v1', $3, 'scope-v1', $4, $5)
                    """,
                    user["omi_uid"],
                    invitation_id,
                    "sha256:" + "2" * 64,
                    "sha256:" + "3" * 64,
                    authority_epoch,
                )
                await conn.execute(
                    """
                    INSERT INTO ella_invitation_redemptions (
                        invitation_id, invitation_target_id, uid_ref_hmac,
                        consent_receipt_ref_hmac, entitlement_revision,
                        support_code, correlation_id
                    ) VALUES ($1, $2, $3, $4, 1, $5, gen_random_uuid())
                    """,
                    invitation_id,
                    target_id,
                    f"{index + 8}" * 64,
                    f"{index + 10:x}" * 64,
                    f"INV-HISTORY-{index}",
                )

            await conn.execute((MIGRATIONS / "015_add_invitation_allowed_email_hash.sql").read_text(encoding="utf-8"))
            rows = await conn.fetch(
                """
                SELECT user_id, user_mapping_state, consent_pending
                FROM ella_invitation_redemptions
                WHERE invitation_id = $1
                ORDER BY support_code
                """,
                invitation_id,
            )
            assert len(rows) == 2
            assert all(row["user_id"] is None for row in rows)
            assert all(row["user_mapping_state"] == "legacy_unmapped" for row in rows)
            assert all(row["consent_pending"] is False for row in rows)
            assert (
                await conn.fetchval(
                    """
                SELECT convalidated
                FROM pg_constraint
                WHERE conname = 'ella_invitation_redemptions_consent_shape_check'
                """
                )
                is True
            )
        finally:
            await conn.close()
            await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
            await admin.close()

    asyncio.run(scenario())
