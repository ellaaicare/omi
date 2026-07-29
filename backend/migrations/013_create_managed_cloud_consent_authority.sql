-- PostgreSQL ordering authority for managed-cloud consent and invitation grants.
--
-- Invariant:
-- - Consent mutation and invitation redemption take the same per-UID advisory
--   transaction lock before changing managed-cloud authority or entitlement.
-- - A grant is usable only when this row is granted and exactly matches the
--   current immutable Firestore receipt/lineage.
-- - Decline/revocation advances authority_epoch and atomically revokes the
--   entitlement/targets and quarantines the Cloud binding.
-- - Firestore remains the receipt store; this row is the transactional ordering
--   authority for PostgreSQL entitlement publication.

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS ella_managed_cloud_consent_authority (
    user_id UUID PRIMARY KEY
        REFERENCES users(id) ON DELETE CASCADE ON UPDATE CASCADE,
    decision TEXT NOT NULL CHECK (decision IN ('granted', 'declined', 'revoked')),
    consent_receipt_ref CHAR(71) COLLATE "C"
        CHECK (
            consent_receipt_ref IS NULL
            OR consent_receipt_ref ~ '^sha256:[0-9a-f]{64}$'
        ),
    profile_binding_id TEXT COLLATE "C",
    policy_version TEXT COLLATE "C",
    processor_set_hash TEXT COLLATE "C"
        CHECK (
            processor_set_hash IS NULL
            OR processor_set_hash ~ '^sha256:[0-9a-f]{64}$'
        ),
    scope_version TEXT COLLATE "C",
    scope_hash TEXT COLLATE "C"
        CHECK (
            scope_hash IS NULL
            OR scope_hash ~ '^sha256:[0-9a-f]{64}$'
        ),
    authority_epoch UUID NOT NULL DEFAULT gen_random_uuid(),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ella_managed_cloud_consent_authority_grant_shape_check CHECK (
        decision <> 'granted'
        OR (
            consent_receipt_ref IS NOT NULL
            AND profile_binding_id IS NOT NULL
            AND policy_version IS NOT NULL
            AND processor_set_hash IS NOT NULL
            AND scope_version IS NOT NULL
            AND scope_hash IS NOT NULL
        )
    )
);

ALTER TABLE voice_entitlements
    ADD COLUMN IF NOT EXISTS consent_authority_epoch UUID;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE connamespace = current_schema()::regnamespace
          AND conname = 'voice_entitlements_invitation_authority_epoch_check'
    ) THEN
        ALTER TABLE voice_entitlements
            ADD CONSTRAINT voice_entitlements_invitation_authority_epoch_check
            CHECK (
                invitation_id IS NULL
                OR consent_authority_epoch IS NOT NULL
            ) NOT VALID;
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS voice_entitlements_consent_authority_epoch_idx
    ON voice_entitlements (consent_authority_epoch)
    WHERE invitation_id IS NOT NULL;

ALTER TABLE ella_runtime_bindings
    DROP CONSTRAINT IF EXISTS ella_runtime_bindings_cloud_target_shape_check;

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
                'hermes-cloud-guardian',
                'hermes-cloud-photon'
            )
        )
    ) NOT VALID;

ALTER TABLE ella_runtime_targets
    DROP CONSTRAINT IF EXISTS ella_runtime_targets_shape_check;

ALTER TABLE ella_runtime_targets
    ADD CONSTRAINT ella_runtime_targets_shape_check CHECK (
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
                'hermes-cloud-guardian',
                'hermes-cloud-photon'
            )
        )
    ) NOT VALID;

COMMIT;
