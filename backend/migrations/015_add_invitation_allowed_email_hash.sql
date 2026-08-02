-- Invitation-only self-hosted launch bindings.
--
-- Migration 011 remains the invitation/capacity authority. This additive
-- migration permits an invitation to be bound to a verified Firebase email at
-- redemption time, reserves exactly one self-hosted runtime target, and models
-- the pre-consent state explicitly. A pending invitation entitlement cannot be
-- used for provisioning until the current consent receipt is published into
-- PostgreSQL and the pending marker is cleared atomically.

BEGIN;

ALTER TABLE ella_invitations
    ADD COLUMN IF NOT EXISTS allowed_email_hash TEXT COLLATE "C"
        CHECK (allowed_email_hash IS NULL OR allowed_email_hash ~ '^[0-9a-f]{64}$');

COMMENT ON COLUMN ella_invitations.allowed_email_hash IS
    'Domain-separated HMAC of a normalized verified Firebase email; NULL means any verified email may redeem.';

ALTER TABLE ella_invitation_targets
    DROP CONSTRAINT IF EXISTS ella_invitation_targets_required_profile_class_check;

ALTER TABLE ella_invitation_targets
    ADD CONSTRAINT ella_invitation_targets_required_profile_class_check
    CHECK (required_profile_class IN ('real', 'synthetic'));

ALTER TABLE voice_entitlements
    ADD COLUMN IF NOT EXISTS invitation_consent_pending BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE voice_entitlements
    DROP CONSTRAINT IF EXISTS voice_entitlements_invitation_authority_epoch_check;

ALTER TABLE voice_entitlements
    ADD CONSTRAINT voice_entitlements_invitation_authority_epoch_check
    CHECK (
        invitation_id IS NULL
        OR (
            invitation_consent_pending = TRUE
            AND consent_authority_epoch IS NULL
        )
        OR (
            invitation_consent_pending = FALSE
            AND consent_authority_epoch IS NOT NULL
        )
    ) NOT VALID;

ALTER TABLE ella_invitation_redemptions
    ADD COLUMN IF NOT EXISTS consent_pending BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id)
        ON DELETE RESTRICT ON UPDATE CASCADE;

UPDATE ella_invitation_redemptions redemption
SET user_id = candidate.user_id
FROM (
    SELECT
        redemption_row.id AS redemption_id,
        (ARRAY_AGG(app_user.id ORDER BY app_user.id))[1] AS user_id
    FROM ella_invitation_redemptions redemption_row
    JOIN voice_entitlements entitlement
      ON entitlement.invitation_id = redemption_row.invitation_id
    JOIN users app_user ON app_user.omi_uid = entitlement.uid
    GROUP BY redemption_row.id
    HAVING COUNT(DISTINCT app_user.id) = 1
) AS candidate
WHERE redemption.id = candidate.redemption_id
  AND redemption.user_id IS NULL;

ALTER TABLE ella_invitation_redemptions
    ALTER COLUMN user_id SET NOT NULL;

ALTER TABLE ella_invitation_redemptions
    ALTER COLUMN consent_receipt_ref_hmac DROP NOT NULL;

ALTER TABLE ella_invitation_redemptions
    DROP CONSTRAINT IF EXISTS ella_invitation_redemptions_consent_shape_check;

ALTER TABLE ella_invitation_redemptions
    ADD CONSTRAINT ella_invitation_redemptions_consent_shape_check
    CHECK (
        (consent_pending = TRUE AND consent_receipt_ref_hmac IS NULL)
        OR
        (consent_pending = FALSE AND consent_receipt_ref_hmac IS NOT NULL)
    );

ALTER TABLE ella_runtime_targets
    ADD COLUMN IF NOT EXISTS invitation_target_id UUID
        REFERENCES ella_invitation_targets(id) ON DELETE RESTRICT ON UPDATE CASCADE;

ALTER TABLE ella_runtime_targets
    DROP CONSTRAINT IF EXISTS ella_runtime_targets_provider_check;

ALTER TABLE ella_runtime_targets
    ADD CONSTRAINT ella_runtime_targets_provider_check
    CHECK (provider IN ('retained', 'hermes_cloud', 'hermes'));

ALTER TABLE ella_runtime_targets
    DROP CONSTRAINT IF EXISTS ella_runtime_targets_status_check;

ALTER TABLE ella_runtime_targets
    ADD CONSTRAINT ella_runtime_targets_status_check
    CHECK (status IN ('reserved', 'ready', 'revoked', 'disabled'));

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
            AND invitation_target_id IS NULL
        )
        OR (
            provider = 'hermes_cloud'
            AND runtime_binding_id IS NOT NULL
            AND candidate_runtime_instance_id IS NOT NULL
            AND endpoint_ref IS NOT NULL
            AND credential_ref IS NOT NULL
            AND invitation_target_id IS NULL
            AND mode IN (
                'hermes-cloud-chat',
                'hermes-cloud-voice',
                'hermes-cloud-transcript',
                'hermes-cloud-guardian',
                'hermes-cloud-photon'
            )
        )
        OR (
            provider = 'hermes'
            AND candidate_runtime_instance_id IS NULL
            AND endpoint_ref IS NULL
            AND credential_ref IS NULL
            AND invitation_target_id IS NOT NULL
            AND mode = 'hermes-chat'
            AND (
                (status = 'reserved' AND runtime_binding_id IS NULL)
                OR (status = 'ready' AND runtime_binding_id IS NOT NULL)
                OR status IN ('revoked', 'disabled')
            )
        )
    ) NOT VALID;

CREATE UNIQUE INDEX IF NOT EXISTS ella_runtime_targets_invitation_target_key
    ON ella_runtime_targets(invitation_target_id)
    WHERE invitation_target_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS ella_runtime_targets_active_hermes_profile_key
    ON ella_runtime_targets(account_user_id, profile_user_id, role, mode)
    WHERE provider = 'hermes' AND status IN ('reserved', 'ready');

ALTER TABLE ella_invitation_audit_receipts
    DROP CONSTRAINT IF EXISTS ella_invitation_audit_receipts_event_type_check;

ALTER TABLE ella_invitation_audit_receipts
    ADD CONSTRAINT ella_invitation_audit_receipts_event_type_check
    CHECK (event_type IN (
        'redeemed', 'idempotent_retry', 'invalid', 'expired', 'capacity',
        'rate_limited', 'policy_invalid', 'redemption_disabled',
        'operator_issued', 'operator_idempotent_retry', 'operator_revoked',
        'operator_cleanup', 'pilot_operator_issued',
        'pilot_operator_idempotent_retry', 'pilot_operator_revoked',
        'pilot_operator_rotated'
    ));

COMMIT;
