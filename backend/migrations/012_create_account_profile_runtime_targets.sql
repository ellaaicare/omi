-- Account/profile-owned runtime targets for authenticated Ella surfaces.
--
-- Contract:
-- - Retained Mini routing is represented by no Cloud target / NULL binding.
-- - Cloud routing is opt-in per account+profile+mode and must point at an exact
--   ready Hermes Cloud binding with endpoint and credential refs.
-- - No global default target is created by this migration.
-- - Existing retained/Plato bindings are not updated.

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

ALTER TABLE ella_runtime_bindings
    ADD COLUMN IF NOT EXISTS account_user_id UUID,
    ADD COLUMN IF NOT EXISTS profile_user_id UUID,
    ADD COLUMN IF NOT EXISTS runtime_target_mode TEXT,
    ADD COLUMN IF NOT EXISTS target_endpoint_ref TEXT,
    ADD COLUMN IF NOT EXISTS target_credential_ref TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE connamespace = current_schema()::regnamespace
          AND conname = 'ella_runtime_bindings_account_user_id_fkey'
    ) THEN
        ALTER TABLE ella_runtime_bindings
            ADD CONSTRAINT ella_runtime_bindings_account_user_id_fkey
            FOREIGN KEY (account_user_id) REFERENCES users(id)
            ON DELETE CASCADE ON UPDATE CASCADE;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE connamespace = current_schema()::regnamespace
          AND conname = 'ella_runtime_bindings_profile_user_id_fkey'
    ) THEN
        ALTER TABLE ella_runtime_bindings
            ADD CONSTRAINT ella_runtime_bindings_profile_user_id_fkey
            FOREIGN KEY (profile_user_id) REFERENCES users(id)
            ON DELETE CASCADE ON UPDATE CASCADE;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE connamespace = current_schema()::regnamespace
          AND conname = 'ella_runtime_bindings_cloud_target_shape_check'
    ) THEN
        ALTER TABLE ella_runtime_bindings
            ADD CONSTRAINT ella_runtime_bindings_cloud_target_shape_check
            CHECK (
                provider <> 'hermes_cloud'
                OR status NOT IN ('shadow', 'internal_canary', 'active')
                OR (
                    user_id IS NOT NULL
                    AND account_user_id IS NOT NULL
                    AND profile_user_id IS NOT NULL
                    AND account_user_id = user_id
                    AND profile_user_id = user_id
                    AND runtime_instance_id IS NOT NULL
                    AND api_base_url_ref IS NOT NULL
                    AND api_key_ref IS NOT NULL
                    AND target_endpoint_ref = api_base_url_ref
                    AND target_credential_ref = api_key_ref
                    AND runtime_target_mode IN (
                        'hermes-cloud-chat',
                        'hermes-cloud-voice',
                        'hermes-cloud-transcript',
                        'hermes-cloud-guardian'
                    )
                )
            ) NOT VALID;
    END IF;
END
$$;

ALTER TABLE ella_runtime_bindings
    DROP CONSTRAINT IF EXISTS ella_runtime_bindings_cloud_pool_shape_check;

ALTER TABLE ella_runtime_bindings
    ADD CONSTRAINT ella_runtime_bindings_cloud_pool_shape_check
    CHECK (
        provider <> 'hermes_cloud'
        OR (
            (
                status = 'pool_available'
                AND user_id IS NULL
                AND account_user_id IS NULL
                AND profile_user_id IS NULL
                AND claim_job_id IS NULL
                AND claim_token IS NULL
                AND active = false
            )
            OR (
                status = 'claiming'
                AND user_id IS NOT NULL
                AND account_user_id IS NULL
                AND profile_user_id IS NULL
                AND claim_job_id IS NOT NULL
                AND claim_token IS NOT NULL
                AND claim_lease_expires_at IS NOT NULL
                AND active = false
            )
            OR (
                status IN ('shadow', 'internal_canary', 'active')
                AND user_id IS NOT NULL
                AND account_user_id IS NOT NULL
                AND profile_user_id IS NOT NULL
                AND runtime_instance_id IS NOT NULL
                AND api_base_url_ref IS NOT NULL
                AND api_key_ref IS NOT NULL
                AND prompt_artifact_receipt <> '{}'::jsonb
                AND active = (status <> 'shadow')
            )
            OR status IN ('quarantined', 'disabled')
        )
    ) NOT VALID;

CREATE INDEX IF NOT EXISTS ella_runtime_bindings_account_profile_idx
    ON ella_runtime_bindings(account_user_id, profile_user_id, provider, status, updated_at)
    WHERE provider = 'hermes_cloud'
      AND account_user_id IS NOT NULL
      AND profile_user_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS ella_runtime_targets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_user_id UUID NOT NULL
        REFERENCES users(id) ON DELETE CASCADE ON UPDATE CASCADE,
    profile_user_id UUID NOT NULL
        REFERENCES users(id) ON DELETE CASCADE ON UPDATE CASCADE,
    role TEXT NOT NULL DEFAULT 'user',
    mode TEXT,
    provider TEXT NOT NULL CHECK (provider IN ('retained', 'hermes_cloud')),
    runtime_binding_id UUID
        REFERENCES ella_runtime_bindings(id) ON DELETE RESTRICT ON UPDATE CASCADE,
    candidate_runtime_instance_id TEXT,
    endpoint_ref TEXT,
    credential_ref TEXT,
    status TEXT NOT NULL DEFAULT 'ready'
        CHECK (status IN ('ready', 'revoked', 'disabled')),
    policy_version TEXT NOT NULL,
    processor_set_hash TEXT NOT NULL CHECK (processor_set_hash ~ '^sha256:[0-9a-f]{64}$'),
    scope_version TEXT NOT NULL,
    scope_hash TEXT NOT NULL CHECK (scope_hash ~ '^sha256:[0-9a-f]{64}$'),
    entitlement_revision INTEGER CHECK (entitlement_revision IS NULL OR entitlement_revision >= 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    revoked_at TIMESTAMPTZ,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT ella_runtime_targets_shape_check CHECK (
        (
            provider = 'retained'
            AND runtime_binding_id IS NULL
            AND candidate_runtime_instance_id IS NULL
            AND endpoint_ref IS NULL
            AND credential_ref IS NULL
            AND mode IS NULL
        )
        OR (
            provider = 'hermes_cloud'
            AND runtime_binding_id IS NOT NULL
            AND candidate_runtime_instance_id IS NOT NULL
            AND endpoint_ref IS NOT NULL
            AND credential_ref IS NOT NULL
            AND mode IN (
                'hermes-cloud-chat',
                'hermes-cloud-voice',
                'hermes-cloud-transcript',
                'hermes-cloud-guardian'
            )
        )
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS ella_runtime_targets_active_cloud_mode_key
    ON ella_runtime_targets(account_user_id, profile_user_id, role, mode)
    WHERE provider = 'hermes_cloud'
      AND status = 'ready';

CREATE UNIQUE INDEX IF NOT EXISTS ella_runtime_targets_active_retained_key
    ON ella_runtime_targets(account_user_id, profile_user_id, role)
    WHERE provider = 'retained'
      AND status = 'ready';

CREATE INDEX IF NOT EXISTS ella_runtime_targets_binding_idx
    ON ella_runtime_targets(runtime_binding_id)
    WHERE runtime_binding_id IS NOT NULL;

COMMIT;
